# 算术单元：arith 命名空间

## 1. 本讲目标

`src/arith/` 是 PoC 的「算术工具箱」。和 `fifo`、`ocram` 这类「拼一个大模块」的命名空间不同，`arith` 里是一堆**互不相干的小算术核**：计数器、优先编码器、伪随机数发生器、开方、宽位加法、除法……它们各自独立，被全库其它核随手取用。

学完本讲，你应当能够：

- 说出 `arith` 命名空间下有哪几类算术核，并知道每类解决什么问题；
- 区分 `arith_counter_free` / `arith_counter_gray` / `arith_counter_ring` / `arith_counter_bcd` 这一组计数器的适用场景，**特别是理解为什么跨时钟域读计数值要用 Gray 码计数器**；
- 读懂 `arith_firstone` 优先编码器「令牌传递 + 进位链」的设计，理解它在固定优先级仲裁里的作用；
- 读懂 `arith_prng`（LFSR 伪随机）、`arith_sqrt`（迭代开方）、`arith_addw`（宽位加法）各自的实现思路；
- 能够仿照官方测试台，自己实例化一个 arith 核。

## 2. 前置知识

本讲假定你已经掌握（若没有，请先学对应讲义）：

- **命名空间包模式（讲义 u3-l1）**：每个命名空间都有一份 `<ns>.pkg.vhdl`「根包」，集中声明组件、类型与函数，且必须**先于**具体核被编译。
- **公共包 `utils`（讲义 u2-l2）**：`log2ceil`、`ite`、`T_NATVEC`、`to_sl` 等会在本讲反复出现。
- **公共包 `config`（讲义 u2-l3）**：`VENDOR` / `VENDOR_XILINX` 枚举被 `arith_firstone` 用来在「通用实现」和「Xilinx 专用实现」之间选择。
- **基础 VHDL**：`std_logic_vector` / `unsigned` / `numeric_std`、`if generate`、`rising_edge` 时钟进程。

再补充三个本讲会用到的硬件概念：

- **LFSR（线性反馈移位寄存器）**：把若干个抽头（tap）位的异或/同或结果反馈回移位寄存器的最低位，就能产生周期极长的伪随机序列，硬件代价极低（一个移位寄存器 + 少量异或门）。
- **Gray 码**：相邻两个码字之间只有 **1 位**不同。跨时钟域传递多比特计数值时，用 Gray 码可以避免「采样到一个从未存在过的中间值」。
- **进位链（carry chain）**：FPGA 里专用的快速逐位进位连线（如 Xilinx 的 `MUXCY` / `CARRY4`），能把加法器或优先编码器的逐位进位压缩到很低的延迟。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/arith/arith.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl) | 命名空间根包：集中声明本命名空间所有核的 component、类型（`tArch`/`tBlocking`/`tSkipping`）与函数（`arith_div_latency`） |
| [src/arith/arith_counter_free.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_free.vhdl) | 自由计数器：每 `DIVIDER` 个 inc 产生 1 拍 strobe，**不输出计数值** |
| [src/arith/arith_counter_gray.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_gray.vhdl) | Gray 码计数器，每步只变 1 位，适合跨时钟域读取 |
| [src/arith/arith_counter_ring.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_ring.vhdl) | 环形 / Johnson 计数器，独热风格 |
| [src/arith/arith_firstone.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl) | 优先编码：找最低位的 1，输出独热授权 + 二进制索引 |
| [src/arith/arith_prng.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl) | LFSR 伪随机数发生器，抽头表来自 Xilinx XAPP052 |
| [src/arith/arith_sqrt.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_sqrt.vhdl) | 迭代开方，N 位被开方数需 `(N+1)/2` 步 |
| [src/arith/arith_addw.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl) | 宽位加法器，提供多种进位选择架构 |
| [tb/arith/arith_prng_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_prng_tb.vhdl) | PRNG 测试台，是「如何实例化一个 arith 核」的范本 |

> 提示：`arith_counter_free.vhdl` 之所以存在，背后有一个重要设计取舍——它刻意不暴露计数值。读懂它之后，你会更清楚为什么旁边还要单独放一个 `arith_counter_gray`。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**计数器族**、**优先编码（arith_firstone）**、**随机与迭代运算（arith_prng / arith_sqrt / arith_addw）**。

### 4.1 计数器族

#### 4.1.1 概念说明

「计数器」听起来简单，但 PoC 在 `src/arith/` 下放了**四种**计数器，因为它们的用途根本不同：

| 核 | 输出形态 | 典型用途 |
|----|----------|----------|
| `arith_counter_free` | 只有 1 拍 `stb` 选通，**没有计数值输出** | 分频器、周期性触发 |
| `arith_counter_gray` | Gray 码计数值 | **跨时钟域**传递计数值（如 FIFO 指针） |
| `arith_counter_ring` | 独热 / Johnson 向量 | 状态数很少的环、移位扫描 |
| `arith_counter_bcd` | 十进制数字（`T_BCD_VECTOR`） | 显示用十进制计数 |

关键直觉是：**输出形态决定了能否被跨时钟域安全读取**。

- `arith_counter_free` 的内部是普通二进制计数器，但作者**故意不把计数值引出来**，只引出一个 1 比特的 `stb`。这样综合器可以放开手脚优化（甚至不需要保留完整的可读计数值），而且这个单比特选通本身就能安全地跨时钟域。
- `arith_counter_gray` 则相反，它**专门**为了「在另一个时钟域读计数值」而生。普通二进制计数器从 `011` 变到 `100` 时，3 个比特几乎同时翻转，但物理上总有先后；另一个时钟域的采样器如果在翻转瞬间采样，就可能采到一个**从未存在过的值**（比如 `111`、`000`）。Gray 码每步只翻转 1 个比特，采样器最多只能采到「旧值」或「新值」二者之一，绝不会采到中间垃圾值。

#### 4.1.2 核心流程

**arith_counter_free（产生周期选通）**

```
输入：inc（每拍可暂停）、DIVIDER（周期）
输出：stb（每 DIVIDER 个有效 inc，拉高正好 1 拍）

N := log2ceil(DIVIDER)            -- 计数位宽
Cnt : N+1 位寄存器，MSB = stb      -- 选通直接取自寄存器最高位
每拍：
  若 inc 无效：Cnt 保持不变（+cin 抵消递减）
  若 MSB=0 且 inc：Cnt 递减 1
  若 MSB=1（刚产生过选通）：Cnt 重载，准备下一轮
稳态：每 DIVIDER 个 inc → MSB 拉高 1 拍 → stb
```

注意三个设计点：①位宽用 `log2ceil(DIVIDER)` 算（u2-l2 学过的函数）；②`stb` 直接来自寄存器位 `Cnt(N)`，时序干净；③`inc='0'` 时计数冻结。

**arith_counter_gray（单比特翻转计数）**

```
维护：gray 计数值寄存器 + 1 个奇偶校验位
每步（inc 有效）：
  只翻转 gray 值中的 1 个比特 → 仍是合法 gray 序列
  → 跨时钟域采样时，读到的要么是旧值要么是新值
dec 有效时反向；inc xor dec 作为使能。
```

它之所以能「只翻 1 位」，靠的是 gray 码的数学性质 + 一个奇偶位来定位该翻哪一位（详见源码）。

**arith_counter_ring（移位环）**

```
寄存器当作移位环：
  inc：左移，最高位回灌到最低位（环形计数器）
       或回灌「最高位取反」（Johnson 计数器，由 INVERT_FEEDBACK 选择）
  dec：反向移位
  reset：载入 seed
```

#### 4.1.3 源码精读

**自由计数器 arith_counter_free**

实体只有一个 generic `DIVIDER` 和一个 `stb` 输出，刻意没有计数值端口——这是它的核心设计取舍（[src/arith/arith_counter_free.vhdl:L41-L53](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_free.vhdl#L41-L53)）：

```vhdl
entity arith_counter_free is
  generic ( DIVIDER : positive );
  port (
    clk : in  std_logic;
    rst : in  std_logic;
    inc : in  std_logic;
    stb : out std_logic   -- End-of-Period Strobe
  );
end entity;
```

实现用 `if generate` 分两种情况（[src/arith/arith_counter_free.vhdl:L65-L93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_free.vhdl#L65-L93)）。`DIVIDER=1` 时，选通就是 `inc` 寄存一拍；`DIVIDER>1` 时才是真正的计数器。关键几行：

```vhdl
constant N : natural := log2ceil(DIVIDER);            -- L76：用 log2ceil 算位宽
signal Cnt : unsigned(N downto 0) := (others => '0');
...
cin(0) <= not inc;                                    -- L81：inc=0 时冻结计数
...
Cnt <= Cnt + ite(Cnt(N) = '0', (Cnt'range => '1'),    -- L88：MSB=0 递减，MSB=1 重载
                 to_unsigned(DIVIDER-1, N+1)) + cin;
...
stb <= Cnt(N);                                        -- L92：选通直接取自寄存器 MSB
```

这里 `(Cnt'range => '1')` 是全 1，在补码下等于 −1，所以「加全 1」就是「减 1」；`ite`（u2-l2 学过的内联三元运算）在「递减」和「重载到 `DIVIDER-1`」之间二选一。`stb` 直接来自寄存器位，所以作者在文件头注释里强调：「guarantees a strobe output directly from a register」（[src/arith/arith_counter_free.vhdl:L16-L18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_free.vhdl#L16-L18)）。

**Gray 码计数器 arith_counter_gray**

实体的 `val` 输出是 Gray 码值（[src/arith/arith_counter_gray.vhdl:L38-L51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_gray.vhdl#L38-L51)）。它先把自然数初值编码成 gray 码（`bin xor (bin 逻辑右移 1 位)`，[src/arith/arith_counter_gray.vhdl:L57-L64](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_gray.vhdl#L57-L64)），并算出其奇偶位。计数寄存器在 `inc xor dec` 使能下更新（[src/arith/arith_counter_gray.vhdl:L88-L98](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_gray.vhdl#L88-L98)）。

多比特版本的次态逻辑（[src/arith/arith_counter_gray.vhdl:L136-L149](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_gray.vhdl#L136-L149)）用了一个技巧：构造向量 `x` 后做 `s := not x + 1` 来定位「第一个中间 1」，从而精确选出本拍该翻转的那一位：

```vhdl
x := gray_cnt_r(BITS-2 downto 0) & (par_r xnor dec);
x(x'left) := not gray_cnt_r(BITS-1);
s := not x + 1;                                       -- 定位要翻转的位
gray_cnt_nxt <= s(BITS-1) & (gray_cnt_r(BITS-2 downto 0) xor
                             (s(BITS-2 downto 0) and x(BITS-2 downto 0)));
```

最终 `gray_cnt_nxt` 相比当前值只会有 **1 位**不同——这正是它跨时钟域安全的根本原因。

**环形 / Johnson 计数器 arith_counter_ring**

实现非常短：一个移位寄存器，反馈位由 `INVERT_FEEDBACK` 决定（[src/arith/arith_counter_ring.vhdl:L63-L74](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_ring.vhdl#L63-L74)）：

```vhdl
elsif (inc = '1') then
  Counter <= Counter(Counter'high - 1 downto 0) & (Counter(Counter'high) xor invert);
```

`invert=0`（默认）时是普通环形计数器（独热位在环上转）；`invert=1` 时回灌取反位，变成 Johnson 计数器（「扭环形」计数，模为 `2*BITS`）。

**BCD 计数器（接口速览）**

`arith_counter_bcd` 输出十进制数字向量，用于显示。我们在根包里看它的声明即可（[src/arith/arith.pkg.vhdl:L56-L64](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L56-L64)），其 `val` 类型为 `T_BCD_VECTOR`，generic `DIGITS` 决定显示几位十进制。本讲不展开它的实现。

#### 4.1.4 代码实践

> **实践目标**：实例化 `arith_counter_free` 实现一个「8 位」周期的自由计数器，并解释它和 Gray 计数器在跨时钟域读取上的差异。

注意 `arith_counter_free` 的 generic 是 `DIVIDER`（周期），**没有位宽端口、也没有计数值输出**。所以「8 位自由计数器」应理解为周期 `2^8 = 256`：每 256 个 `inc` 产生 1 拍选通。

**操作步骤（仿照 [tb/arith/arith_prng_tb.vhdl:L84-L101](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_prng_tb.vhdl#L84-L101) 的写法）**：

1. 新建一个测试台文件 `arith_counter_free_tb.vhdl`（示例骨架，非项目原有代码）：

```vhdl
-- 示例代码：测试台骨架
library IEEE;
use     IEEE.std_logic_1164.all;
library PoC;
use     PoC.physical.all;
use     PoC.simulation.all;        -- 仿真辅助包（见 u4-l1）

entity arith_counter_free_tb is end entity;

architecture tb of arith_counter_free_tb is
  constant CLOCK_FREQ : FREQ := 100 MHz;
  signal Clock, Reset, inc, stb : std_logic;
begin
  simInitialize;
  simGenerateClock(simTestID, Clock, CLOCK_FREQ);
  simGenerateWaveform(simTestID, Reset, simGenerateWaveform_Reset(Pause => 10 ns, ResetPulse => 10 ns));

  -- 「8 位」=> 周期 256
  UUT : entity PoC.arith_counter_free
    generic map ( DIVIDER => 256 )
    port map ( clk => Clock, rst => Reset, inc => inc, stb => stb );

  -- 让 inc 持续为 1
  inc <= '1';
end architecture;
```

2. 把它放进 `tb/arith/`，并仿照 `src/arith/arith_counter_free.files`（[src/arith/arith_counter_free.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_free.files)）配套一份 `tb/arith/arith_counter_free_tb.files`，先 `include "src/common/common.files"`，再编译 `arith.pkg.vhdl` 与 `arith_counter_free.vhdl`。
3. 用 pyIPCMI / GHDL 跑仿真。

**需要观察的现象**：

- `stb` 应每 256 个 `Clock` 上升沿拉高正好 1 拍；
- `stb` 来自寄存器，无组合毛刺；
- `inc='0'` 期间计数冻结（可临时把 `inc <= '1';` 改成带暂停的波形验证）。

**预期结果**：稳态下选通周期 = 256 个有效 `inc`（首拍因 reset 初值可能略早，属正常）。**精确的首拍时序待本地验证**。

**与 Gray 计数器的差异（本实践的核心问题）**：

- `arith_counter_free` **没有计数值输出**，只有单比特 `stb`。单比特信号跨时钟域只需要一个 2 级 FF 同步器即可（这也是 u3-l6 `sync_Bits` 的活），但它**不能告诉你「现在数到第几个」**。
- 如果你需要的是「在另一个时钟域读到当前计数值」（最典型的例子就是跨钟 FIFO 的读写指针），就必须用 `arith_counter_gray`：它输出 Gray 码值，每步只变 1 位，跨域采样安全。换 `arith_counter_free` 的二进制内部值去跨域读，会在翻转瞬间采到垃圾值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `arith_counter_free` 不把内部二进制计数值引到端口？

> **参考答案**：因为引出计数值会约束综合器——它必须保留一个完整、可读、行为精确的二进制计数器。只要外部只关心「每 DIVIDER 拍一个选通」，不引出值就能让综合器自由优化（位宽、编码方式都可裁剪），同时单比特 `stb` 本身跨时钟域也更安全。

**练习 2**：`arith_counter_gray` 用了 `log2ceil` 吗？它和 `arith_counter_free` 的位宽算法一样吗？

> **参考答案**：`arith_counter_gray` 的位宽直接由 generic `BITS` 给定（见 [L38-L51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_gray.vhdl#L38-L51)），不需要 `log2ceil`；而 `arith_counter_free` 由 `DIVIDER` 用 `log2ceil(DIVIDER)` 推算位宽（[L76](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_counter_free.vhdl#L76)）。两者端口哲学不同：前者显式给位宽并输出值，后者给周期且不输出值。

**练习 3**：把 `arith_counter_ring` 的 `INVERT_FEEDBACK` 设为 `TRUE`，计数器的模（周期长度）会变成多少？

> **参考答案**：变成 Johnson（扭环）计数器，模为 `2 * BITS`。因为反馈位取反，状态在「全 0 → 逐位填 1 → 全 1 → 逐位填 0」之间循环，共 `2*BITS` 个状态。

---

### 4.2 优先编码：arith_firstone

#### 4.2.1 概念说明

优先编码（priority encoder）解决的问题是：**给定一个请求向量 `rqst`，找出其中「最低位的 1」**，输出一个独热（one-hot）的授权向量 `grnt`，以及这个位的二进制索引 `bin`。

PoC 的 `arith_firstone` 把它包装成了「令牌传递」的接口，专门服务于**固定优先级仲裁**：想象一条链，令牌从低位端 `tin` 进入，沿着请求位逐级传递，遇到第一个发请求的就停下来授权，没传完的令牌从 `tout` 出去。这样：

- 设 `tin='1'`（有令牌输入）才允许授权；`tin='0'` 一律不授权；
- `tout='1'` 表示令牌没用掉（这一轮没有任何请求）；
- `bin` 给出被授权者的二进制编号（从 0 起）。

和「先到先得」的公平仲裁不同，`arith_firstone` 的优先级是**固定且严格**的：低位永远比高位优先。这正是「优先编码器」的天职。

#### 4.2.2 核心流程

```
输入：tin（令牌输入），rqst[N-1:0]（请求向量）
输出：grnt[N-1:0]（独热授权），tout（未用令牌），bin（授权编号）

「找最低位的 1」= 「计算 rqst 上比它更低的位都不请求时的累积进位」
通用实现：把 ~rqst 当作操作数，加上 tin，进位传播天然形成「扫描」：
  adder = ("0" & unsigned(not rqst)) + tin
  最高位 adder(N) = tout（令牌是否穿透）
  onehot = adder(N-1:0) and rqst   ← 只在「进位首次撞上请求位」处为 1
  bin    = onehot 的二进制编码

Xilinx 专用实现（N>=6）：用 MUXCY 原语搭一条专用进位链，延迟更低。
```

两种实现用 `if generate` 二选一，守卫条件就是 u2-l3 学到的 `VENDOR`。

#### 4.2.3 源码精读

实体的端口直接体现「令牌传递」语义（[src/arith/arith_firstone.vhdl:L50-L61](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L50-L61)）。注意 `bin` 的宽度由 `log2ceil(N)` 决定。

通用实现（非 Xilinx 或 `N<6`，[src/arith/arith_firstone.vhdl:L73-L91](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L73-L91)）用一个加法器完成扫描，非常巧妙：

```vhdl
adder  := ("0" & unsigned(not rqst)) + (1 to 1 => tin);   -- L79
onehot := std_logic_vector(adder(N-1 downto 0)) and rqst; -- L80
...
tout <= adder(N);                                          -- L87
grnt <= onehot;                                            -- L88
bin  <= std_logic_vector(binary);                          -- L89
```

为什么加法能「扫描」？因为 `~rqst` 在无请求位上是 1、请求位上是 0；加 `tin`（0 或 1）后，进位会在遇到第一个 0（即第一个请求位）时停止传播。于是 `adder` 的每一位反映「令牌是否传到了这里」，再和 `rqst` 相与，就只剩下「第一个被授权的请求位」——这正是独热授权。二进制索引 `bin` 则由一个 `for` 循环对独热向量编码（[L82-L86](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L82-L86)）。

Xilinx 专用实现（`VENDOR = VENDOR_XILINX` 且 `N >= 6`，[src/arith/arith_firstone.vhdl:L94-L167](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L94-L167)）把上面的「加法进位」替换成显式实例化的 `MUXCY` 进位链原语，让综合器把它映射到 FPGA 的专用进位连线，从而在宽位宽下仍保持低延迟。守卫条件 `VENDOR /= VENDOR_XILINX or N < 6` 与 `VENDOR = VENDOR_XILINX and N >= 6`（[L73](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L73) 与 [L94](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L94)）正是 u3-l2 讲的「展开期 generate 分发 + `VENDOR` 守卫」模式。

#### 4.2.4 代码实践

> **实践目标**：用源码阅读 + 手工演算，验证「加法扫描」优先编码的正确性。

**操作步骤**：

1. 打开 [src/arith/arith_firstone.vhdl:L73-L91](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L73-L91)，取 `N=8`。
2. 设 `tin='1'`，`rqst = "00010100"`（即位 2 和位 4 有请求，位 2 更低）。手工算 `~rqst = "11101011"`，`adder = 0b0_11101011 + 1 = 0b0_11101100`。
3. `onehot = adder(7:0) and rqst = "11101100" and "00010100" = "00000100"` —— 只有位 2 为 1，正是最低位的请求。
4. `tout = adder(8) = 0`（令牌被用掉了）；`bin` 编码 `onehot` 得到 `010`（=2）。

**需要观察的现象**：`onehot` 恰好在最低请求位为 1、其余为 0；`tout` 为 0 表示有授权发生。

**预期结果**：与上一步手工演算一致。若把 `rqst` 改成全 0，则 `adder = ~0 + 1` 进位穿透，`tout=1`（无请求），`onehot=0`。**完整仿真波形待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么「找最低位的 1」可以用「`~rqst + tin` 的进位」来实现？

> **参考答案**：`~rqst` 在请求位上是 0、非请求位上是 1。给最低位加 `tin` 后，进位沿着为 1 的位（非请求位）逐级向上传播，遇到第一个为 0 的位（即第一个请求位）时进位被「吸收」而停止。所以 `adder` 的进位状态天然标记了「令牌传到哪」，与 `rqst` 相与后只剩第一个请求位。

**练习 2**：Xilinx 分支（`genXilinx`）为什么要单独存在？它和通用分支的取舍条件是什么？

> **参考答案**：当 `N` 较大时，通用分支的加法器延迟会随位宽增长；Xilinx 分支用 `MUXCY` 原语显式搭专用进位链，映射到 FPGA 硬进位资源，宽位宽下延迟更低。取舍条件是 `VENDOR = VENDOR_XILINX and N >= 6`（[L94](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_firstone.vhdl#L94)）：只有 Xilinx 器件且位宽足够时才值得用专用链，否则用通用加法实现。

---

### 4.3 随机与迭代运算：arith_prng / arith_sqrt / arith_addw

#### 4.3.1 概念说明

这一组是「运算核」，三者风格各异：

- **`arith_prng`**：伪随机数发生器。用一个 LFSR 产生周期极长的 0/1 序列，每个 `got`（消费）脉冲推进一步、吐出一个新的 `BITS` 位随机数。硬件代价极低。
- **`arith_sqrt`**：整数开方。给定 `N` 位被开方数 `arg`，在 `start` 选通后经 `(N+1)/2` 个时钟步迭代求出平方根，`rdy` 拉高表示完成。它是「多周期迭代」式运算核的典型。
- **`arith_addw`**：宽位加法器。当加法位宽很大（远超单个进位链长度）时，简单的行波进位太慢；`arith_addw` 用「进位选择（carry-select）」思路把加法拆成 `K` 个块，每块同时算 `cin=0/1` 两种结果再 mux，从而把延迟从 `O(N)` 降到接近 `O(K)`。

三者都体现了 PoC 的一个共性：**把可参数化的硬件算法封装成一个核，供全库复用**。

#### 4.3.2 核心流程

**arith_prng（LFSR 伪随机）**

```
维护：BITS 位移位寄存器 val_r，初值 = SEED
每拍（got=1）：
  bit1 = val_r 最高位 XNOR 各抽头位       -- 反馈位（XNOR 使全 0 合法、全 1 非法）
  val_r <= val_r 左移 1 位，最低位填 bit1  -- 推进一步
输出：val = val_r
复位：回到 SEED
```

抽头位置来自一张预计算表（Xilinx XAPP052），覆盖 3~168 位、每条多项式最多 5 个抽头，使综合器能把长 LFSR 推断成移位寄存器（SRL）而非一堆独立触发器。

**arith_sqrt（迭代开方）**

```
STEPS = (N+1)/2                          -- 结果位数 = 迭代步数
start：载入 arg 到余数寄存器 Rmd，置 Vld 全 1
每步（Vld 还没移空）：
  试减 diff = 4*Rmd - (4*当前结果+1)
  若 diff>=0（试减成功）：本位结果 = 1，余数替换为移位后的 diff
  否则（失败）：本位结果 = 0，余数仅移位
  Vld 右移一位
rdy = Vld 最高位取反                      -- 全部移完即完成
```

**arith_addw（进位选择宽加法）**

```
把 N 位加法切成 K 块（块边界由 compute_blocks 按位宽和架构算出）
最右块 + 进位核心：用 FPGA 进位链（CCC）或 LUT 链算出块间进位 c[]
其余每块：同时算 (a+b+0) 和 (a+b+1) 两种和，按进入本块的进位 c(i) mux 出结果
架构选项 ARCH ∈ {AAM, CAI, CCA, PAI} 表达「如何算这两种和 / 是否真算两次」
```

#### 4.3.3 源码精读

**arith_prng**

实体很简单（[src/arith/arith_prng.vhdl:L46-L57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L46-L57)）：`BITS` 决定随机数位宽，`SEED` 决定初值，`got` 是「消费一个、推进一次」的握手。

抽头表 `C_TAPPOSITION_LIST` 是全核最长的常量（[src/arith/arith_prng.vhdl:L65-L232](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L65-L232)），覆盖 3 到 168 位，注释指明来源是 Xilinx XAPP052（[L64](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L64)）。当前位宽对应的抽头由 `C_TAPPOSITIONS : T_TAPPOSITION := C_TAPPOSITION_LIST(BITS)` 一次性查表得到（[L234](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L234)）；`assert` 把位宽限制在 3~168（[L241](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L241)）。

反馈与移位是核心（[src/arith/arith_prng.vhdl:L247-L271](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L247-L271)）：

```vhdl
-- 反馈位：XNOR 使 all-zero 合法、all-one 非法
temp := val_r(val_r'left);
for i in 0 to 4 loop
  if C_TAPPOSITIONS(i) > 0 then
    temp := temp xnor val_r(C_TAPPOSITIONS(i));
  end if;
end loop;
bit1_nxt <= temp;
...
-- got 时左移、最低位填反馈
val_r <= val_r(val_r'left - 1 downto 1) & bit1_nxt;
```

注释（[L246](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L246)）解释了为什么用 XNOR 而不是 XOR：XNOR 反馈下「全 0」是合法状态、「全 1」是非法状态，作者据此在文件头声明「序列包含全 0、不包含全 1」（[L18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L18)）。官方测试台正是用 `SEED=0x12`、`BITS=8` 把输出和 256 个预计算值逐一比对（[tb/arith/arith_prng_tb.vhdl:L56-L73](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_prng_tb.vhdl#L56-L73)、[L91-L101](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_prng_tb.vhdl#L91-L101)）。

**arith_sqrt**

迭代步数 `STEPS = (N+1)/2`（[src/arith/arith_sqrt.vhdl:L61](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_sqrt.vhdl#L61)），即「结果位数 = 被开方数位宽的一半」。主进程（[src/arith/arith_sqrt.vhdl:L74-L113](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_sqrt.vhdl#L74-L113)）在 `start` 时载入 `arg`、把有效位 `Vld` 置全 1，之后每拍做一次「试减 + 移位」，并把 `Vld` 右移一位当作「还剩几步」的计数。试减式（[L121](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_sqrt.vhdl#L121)）：

```vhdl
diff <= Rmd(Rmd'left downto N-2) + ('1' & not Res(STEPS-2 downto 0) & "11");
```

结果位在迭代中由 `Res(i) <= Rmd(2*i) and not Vld(i)` 提取（[L116-L118](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_sqrt.vhdl#L116-L118)），`rdy <= not Vld(Vld'left)`（[L125](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_sqrt.vhdl#L125)）在所有步数走完时拉高。这是典型的「握手启动 + 多周期迭代 + 完成选通」时序。

**arith_addw**

generic 把架构选择做成了枚举旋钮（[src/arith/arith_addw.vhdl:L54-L71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L54-L71)），类型 `tArch`/`tBlocking`/`tSkipping` 在根包里声明（[src/arith/arith.pkg.vhdl:L161-L163](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L161-L163)）。块边界由一个 `impure function compute_blocks` 在展开期算出（[src/arith/arith_addw.vhdl:L86-L137](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L86-L137)）。

进位核心用 FPGA 进位链实现块间进位（`SKIPPING = CCC`，[src/arith/arith_addw.vhdl:L151-L173](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L151-L173)）；每个非最右块用进位选择：以最直观的 `AAM`（Add-Add-Multiplex）为例，它同时算 `cin=0` 和 `cin=1` 两种和，再用进入本块的进位 `c(i)` 选一个（[src/arith/arith_addw.vhdl:L283-L298](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L283-L298)）：

```vhdl
s0 <= ('0' & aa) + bb;          -- cin=0 的和
s1 <= ('0' & aa) + bb + 1;      -- cin=1 的和
...
ss <= s0(HI downto LO) when c(i) = '0' else s1(HI downto LO);  -- mux
```

其余架构 `CAI`/`CCA`/`PAI`（[L301-L347](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L301-L347)）是「省一次加法、用比较代替」的变体，文件头注释（[L16-L27](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L16-L27)）列出了对应论文。

> 旁注：根包里还有一个相关核 `arith_div`（[src/arith/arith.pkg.vhdl:L87-L110](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L87-L110)），以及计算其延迟的函数 `arith_div_latency`（声明 [L85](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L85)，实现 `return (a_bits+rapow-1)/rapow` 在 [L239-L242](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L239-L242)）——它和 `arith_sqrt` 同属「多周期迭代运算」一类，本讲不展开。

#### 4.3.4 代码实践

> **实践目标**：实例化 `arith_prng`，复现官方测试台的种子序列，理解 LFSR 的推进节奏。

**操作步骤**：

1. 直接运行项目自带的 [tb/arith/arith_prng_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_prng_tb.vhdl)（它已经把 `BITS=8`、`SEED=x"12"` 的实例化、时钟/复位生成、逐拍比对都写好了）。
2. 若要自己改参数体验：把 `BITS` 改成 `16`、`SEED` 改成 `x"1234"`，并据此重算期望序列（或先放宽断言只看波形）。

**需要观察的现象**：

- 复位释放后，每来一个 `got` 上升沿，`val` 在下一个时钟沿更新为新的伪随机数；
- 首个值应等于 `SEED`（`0x12`）本身，其后依次是 `0x24, 0x48, 0x90, ...`（对照 [tb/arith/arith_prng_tb.vhdl:L56-L73](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/arith/arith_prng_tb.vhdl#L56-L73) 的预计算表）；
- `got='0'` 期间 `val` 保持不变。

**预期结果**：默认参数下，测试台跑完 256 拍无断言失败，说明 LFSR 推进与预计算多项式一致。**改参数后的精确序列待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`arith_prng` 为什么用 XNOR 而不是 XOR 做反馈？

> **参考答案**：反馈函数决定了哪个状态「非法」（吸收态）。用 XNOR 时「全 1」是非法态、「全 0」合法，于是序列包含全 0；这与文件头声明一致（[L18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_prng.vhdl#L18)）。若用 XOR 则全 0 会是死态，序列会卡死。

**练习 2**：`arith_sqrt` 对一个 16 位被开方数，要多少个时钟周期出结果？`rdy` 何时拉高？

> **参考答案**：`STEPS = (16+1)/2 = 8`，所以从 `start` 算起需 8 个迭代步。`rdy = not Vld(Vld'left)`，当 `Vld` 寄存器全部右移殆尽（最高位变 0）时 `rdy` 拉高，表示结果 `sqrt` 已就绪（[L125](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_sqrt.vhdl#L125)）。

**练习 3**：`arith_addw` 的 `AAM` 架构里，每个非最右块为什么算了两次加法？

> **参考答案**：因为进入该块的进位 `c(i)` 要等最右块的进位链算完才知道，会来得晚。为了让本块的「求和」不串行等待进位，干脆**同时**算 `cin=0`（`s0`）和 `cin=1`（`s1`）两种和，等 `c(i)` 到了再用一个 2 选 1 mux 选出结果——用面积换延迟，这就是 carry-select 的核心思想（[L283-L298](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L283-L298)）。

---

## 5. 综合实践

把本讲三个模块串起来，设计一个**「4 路固定优先级仲裁 + 随机激励」**的小系统（纯源码阅读 + 骨架设计，不必上板）：

1. **激励源**：实例化一个 `arith_prng`（`BITS=4`），用它的低 4 位作为 4 个请求者的随机请求向量 `rqst`。
2. **仲裁**：把 `rqst` 喂给一个 `arith_firstone`（`N=4`，`tin='1'`），用它的 `grnt` 选出本轮获准者、`bin` 作为编号。
3. **统计**：用一个 `arith_counter_free`（`DIVIDER` 取一个方便观察的值，如 1024）产生周期选通，每个选通拍把当次 `bin` 锁存下来观察。
4. **思考题**：如果你想让「获准次数」跨时钟域被另一个域读到，应该用 `arith_counter_free` 还是 `arith_counter_gray`？为什么？

**预期收获**：你会看到 `prng`（随机）、`firstone`（优先编码）、`counter_free`（计数器族）三者如何用 PoC 一致的「generic 配置 + 握手端口」风格拼在一起；并亲手验证「单比特选通可跨域、多比特计数值要用 Gray 码」这条本讲核心结论。**完整时序与统计结果待本地仿真验证**。

## 6. 本讲小结

- `src/arith/` 是一堆**互不相干的算术小核**，统一在根包 `arith.pkg.vhdl` 里声明，按命名空间包模式（u3-l1）先于具体核编译。
- **计数器族按「输出形态」分家**：`free` 只给选通不给值、`gray` 给 Gray 码值（跨时钟域安全）、`ring` 给独热/Johnson 向量、`bcd` 给十进制数字。
- `arith_counter_free` 刻意不暴露计数值，`stb` 直接取自寄存器 MSB，位宽用 `log2ceil(DIVIDER)` 推算。
- 跨时钟域读**计数值**要用 `arith_counter_gray`（每步只翻 1 位）；只传**单比特选通**用 `arith_counter_free` + 同步器即可。
- `arith_firstone` 把优先编码包装成「令牌传递」接口，通用实现用「`~rqst + tin` 进位扫描」，Xilinx 实现用 `MUXCY` 进位链，靠 `VENDOR` 守卫二选一（u3-l2 的可移植模式）。
- `arith_prng` 是 XNOR 反馈 LFSR（抽头来自 XAPP052，3~168 位），`arith_sqrt` 是 `(N+1)/2` 步迭代开方，`arith_addw` 是 carry-select 宽位加法（架构由 `tArch` 枚举切换）。

## 7. 下一步学习建议

- **学跨时钟域同步器**：本讲反复提到「Gray 计数器要配同步器」，下一步请学讲义 **u3-l6（时钟域穿越：misc/sync）**，看 `sync_Bits` / `sync_Vector` 如何把单比特和多比特信号安全地搬到另一个时钟域，并理解 `SYNC_DEPTH` 与 `_meta`/`_async` 约束。
- **看 Gray 码的真实用武之地**：讲义 **u3-l4（FIFO 家族）** 里的 `fifo_ic_got` 跨钟 FIFO 正是用 Gray 指针 + 双 FF 同步实现读写指针比较——那是 `arith_counter_gray` 最经典的落地场景。
- **进阶运算核**：如果对迭代运算感兴趣，可自行阅读 `src/arith/arith_div.vhdl`（除法）与 `src/arith/arith_scaler.vhdl`（时钟分频Scaler），它们和 `arith_sqrt` 同属「握手启动 + 多周期迭代 + 完成选通」一类。
- **补充开方/加法的算法背景**：`arith_addw.vhdl` 文件头列出了两篇 FPL 论文（[L16-L27](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L16-L27)），想深究 carry-select 与并行前缀网络的读者可据此扩展阅读。
