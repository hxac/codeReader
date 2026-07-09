# LFSR 伪随机与环形缓冲

## 1. 本讲目标

本讲讲解 hdl-modules 里两个「轻量但极常用」的构建块：

- **LFSR（线性反馈移位寄存器）**：用最简单的移位加异或结构，产生周期极长、统计上近似随机的序列，常用于伪随机数、数据扰码（scrambling）、唯一计数、测试激励等。
- **环形缓冲（ring buffer / circular buffer）**：FPGA 持续向一段内存写数据、CPU 逐步读走数据的「生产者—消费者」地址管理器，是 DMA、视频采集等场景的核心骨架。

学完本讲你应该能够：

1. 说清「最大长度 LFSR」为什么周期是 \(2^{n}-1\)，以及全零状态为什么是禁区。
2. 读懂 `lfsr_pkg` 里的抽头表，并能用 `get_lfsr_taps` 手工验证一个抽头位掩码。
3. 区分 `lfsr_fibonacci_single`（每拍移 1 位、能映射到 SRL）与 `lfsr_fibonacci_multi`（每拍移多位、不能映射到 SRL）的结构差异与资源代价。
4. 读懂 `ring_buffer_write_simple` 的环形地址管理：对齐地址、写指针回绕、以及「永远空一格以区分满与空」的设计。
5. 在 testbench 里实例化二者并观察周期性、回绕行为。

## 2. 前置知识

本讲依赖 u2-l2 讲过的两个基础包，先快速回顾：

- `common.types_pkg`：`natural_vec_t`（natural 数组）、`slv_vec_t`、`u_unsigned`（项目里通用的无符号数值类型，写法是 `u_unsigned(width-1 downto 0)`）。
- `math.math_pkg`：本讲用到 `ceil_log2(x)`（向上取整以 2 为底的对数，用来算「地址里有多少低位必然为 0」）和 `is_power_of_two(x)`（判断是否 2 的幂）。

另外还要回忆两个本手册反复强调的约定：

- **AXI-Stream 式握手**（u2-l1）：`valid`/`ready` 同拍同时为 1 才完成一次 beat；`valid` 不得组合依赖 `ready`。本讲的 `ring_buffer_write_simple` 就用这套握手把「段地址」一段段发给用户。
- **用 generic 裁剪功能、综合时未启用的 `generate` 块零资源**（u1-l1 的设计哲学）。

两个属于本讲的新概念：

- **伪随机（pseudo-random）**：序列看起来随机，其实由确定性的递推公式产生；只要种子和反馈多项式相同，序列完全可复现。
- **环形缓冲 / 循环缓冲（circular buffer）**：一段固定大小的地址空间，写指针写到底再回头从起点继续写，像一条首尾相接的传送带。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [modules/lfsr/src/lfsr_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_pkg.vhd) | LFSR 生态的「字典」：2 到 64 位的最大长度抽头表，以及两个精化期函数 `get_lfsr_taps`、`get_required_lfsr_length`。 |
| [modules/lfsr/src/lfsr_fibonacci_single.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_fibonacci_single.vhd) | 单比特输出、每拍移 1 位的 Fibonacci LFSR，是 `lfsr_fibonacci_multi` 的薄封装，能映射到 SRL。 |
| [modules/lfsr/src/lfsr_fibonacci_multi.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_fibonacci_multi.vhd) | 多比特输出、每拍移多位的 Fibonacci LFSR，是 LFSR 真正的实现核心。 |
| [modules/ring_buffer/src/ring_buffer_write_simple.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple.vhd) | 环形缓冲写端的地址管理器：发段地址、收写完成、维护写指针回绕。 |
| [modules/ring_buffer/src/ring_buffer_write_simple_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple_pkg.vhd) | 环形缓冲的状态 record 类型与两个常量初值。 |
| [modules/lfsr/test/tb_lfsr.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/test/tb_lfsr.vhd) | LFSR 测试台：采 \(2^{n}-1\) 个样本落盘，配合 Python 后检查做频谱验证。 |
| [modules/ring_buffer/test/tb_ring_buffer_write_simple.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/test/tb_ring_buffer_write_simple.vhd) | 环形缓冲测试台：随机读进度 + 随机背压，验证地址递增与回绕、永不越界。 |

> 提示：`lfsr` 与 `ring_buffer` 各自的 `module_*.py`（`module_lfsr.py`、`module_ring_buffer.py`）负责把仿真配置与 netlist 资源回归接进来，本讲会借用它们来读「资源数字」与「验证策略」，但它们的机制本身属于 u1-l4 与 u8-l3 的范畴。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **LFSR 数学基础与抽头表**（`lfsr_pkg`）
2. **Fibonacci LFSR 的实现：single 与 multi**
3. **环形缓冲写端：`ring_buffer_write_simple`**

### 4.1 LFSR 数学基础与抽头表（lfsr_pkg）

#### 4.1.1 概念说明

LFSR 是一个带「反馈」的移位寄存器：每个时钟拍，寄存器整体移一位，新移进来的那个比特是寄存器里若干「抽头（tap）」位置的异或（XOR）。因为反馈是线性的（只有异或），所以叫「线性反馈」。

它有两种等价结构：

- **Fibonacci 结构**：反馈结果从一端移入，输出取另一端。本讲实现的就是这种。
- **Galois 结构**：反馈作用在每一位上。本模块未实现。

「最大长度 LFSR（maximum-length LFSR）」是指：选对了抽头位置后，序列会遍历除全零外的所有状态再回到起点。一个 \(n\) 位最大长度 LFSR 的周期为：

\[
\text{period} = 2^{n} - 1
\]

为什么是 \(2^{n}-1\) 而不是 \(2^{n}\)？因为 **全零状态是锁死状态**：所有位都是 0，异或结果永远是 0，序列会永远停在 0。所以合法状态只有 \(2^{n}-1\) 个非零状态。这一点在源码里被一条断言守护（见 4.2.3）。

抽头位置不是随便选的——它对应一个「本原多项式（primitive polynomial）」。每个位宽 \(n\) 都有一组已知能产生最大长度的抽头。hdl-modules 把这些抽头预先存成一张表，省得用户去查多项式。

#### 4.1.2 核心流程

`lfsr_pkg` 提供三样东西：

1. **抽头表** `non_zero_tap_table`：以位宽 \(n\)（2 到 64）为索引，每个条目存最多 5 个非零抽头位置（高位/输出位是隐含的、不入表，省一格）。
2. **`get_lfsr_taps(n)`**：把表里某一行翻译成一个长度为 \(n\) 的位掩码 `std_ulogic_vector(n downto 1)`，某位为 `'1'` 表示这个位置参与异或。它总是把最高位置 `'1'`（Fibonacci 结构里高位即输出位，永远参与反馈）。
3. **`get_required_lfsr_length(shift_count, minimum_length)`**：为「每拍移 `shift_count` 位」的需求，挑出最小的、且最低抽头位置 ≥ `shift_count` 的 LFSR 长度。这个约束是为了让多步移位的异或方程不至于太复杂（见 4.2.2）。

把表「翻译成掩码」的直觉：

```
查表 non_zero_tap_table(n) → 得到非零抽头列表（不含最高位）
把列表里每个抽头对应的位置 '1'
再把最高位置 '1'（输出位恒参与反馈）
得到一个 n 位的 "反馈掩码"
```

#### 4.1.3 源码精读

先看表本身。注释里有一条很实在的工程经验：抽头值在 Xilinx 应用笔记和 Wikipedia 之间对 12、13、14、19 有出入，「两组都被验证可用」，项目统一采用 Xilinx 的值（位宽 2 用 Wikipedia 的）——这是「以可运行验证为准」的体现。

抽头表（节选）：[modules/lfsr/src/lfsr_pkg.vhd:34-105](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_pkg.vhd#L34-L105)

```vhdl
-- We do not include the input and output bits in the table, to save space.
-- They are implied.
-- The value of 0 means unused.
constant non_zero_tap_table : non_zero_tap_table_t := (
  2 => (1, 0, 0, 0, 0),
  3 => (2, 0, 0, 0, 0),
  ...
  15 => (14, 0, 0, 0, 0),
  16 => (15, 13, 4, 0, 0),
  ...
  64 => (63, 61, 60, 0, 0)
);
```

类型 `non_zero_taps_t is natural_vec_t(0 to 4)`（最多 5 个抽头），表类型是 `array(2 to 64)`。`0` 表示「未用」。

`get_lfsr_taps` 的实现，关键是注释里那句「高位恒为 1，所以表里能省一个数」：[modules/lfsr/src/lfsr_pkg.vhd:122-145](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_pkg.vhd#L122-L145)

```vhdl
variable result : std_ulogic_vector(lfsr_length downto 1) := (others => '0');
...
-- Fibonacci needs the high bit to be 1, and it does not matter for Galois.
-- Hence we can set it always to one, which means we can save one number in the table.
result(result'high) := '1';

for tap_idx in result'range loop
  for non_zero_tap_idx in non_zero_taps'range loop
    if tap_idx = non_zero_taps(non_zero_tap_idx) then
      result(tap_idx) := '1';
    end if;
  end if;
end loop;
```

验证一下：位宽 5 时表项是 `(3,0,0,0,0)`，加上恒为 1 的最高位 5，掩码就是 `bit5=1, bit3=1, 其余 0`，即 `"10100"`（5 downto 1）。这与测试台 `tb_lfsr_pkg` 里写死的期望值完全一致——`tb_lfsr_pkg.vhd` 正是用四个固定向量来反向校验这张表：

```vhdl
constant taps5 : std_ulogic_vector(5 downto 1) := "10100";
...
check_equal(get_lfsr_taps(5), taps5);
```
见 [modules/lfsr/test/tb_lfsr_pkg.vhd:32-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/test/tb_lfsr_pkg.vhd#L32-L46)。

`get_required_lfsr_length` 的逻辑是「扫一遍表，找到第一个满足『所有非零抽头都 ≥ shift_count』且 ≥ minimum_length 的位宽」：[modules/lfsr/src/lfsr_pkg.vhd:147-174](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_pkg.vhd#L147-L174)。找不到就 `assert ... severity failure` 直接报错（这是项目用断言固化设计前提的一贯做法，参见 u2-l3、u4-l1）。

Python 侧 `module_lfsr.py` 的 `post_check_lfsr_pkg` 还对这张表做了「必要非充分」的数学健全性检查：抽头个数必须为偶数、且两两互质（gcd=1）——这是最大长度 LFSR 多项式的必要条件。见 [modules/lfsr/module_lfsr.py:70-93](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/module_lfsr.py#L70-L93)。这种「VHDL 仿真采数据 + Python 后检查做数学验证」的双层校验，是本模块测试方法论的核心（详见 4.2.4）。

#### 4.1.4 代码实践

**目标**：手工把抽头表翻译成掩码，验证自己理解了 `get_lfsr_taps`。

**操作步骤**：

1. 打开 [modules/lfsr/src/lfsr_pkg.vhd:34-105](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_pkg.vhd#L34-L105)。
2. 选位宽 13，读出表项 `(4,3,1,0,0)`。
3. 自己写出 13 位的掩码（bit13 恒 1，再加上 bit4、bit3、bit1）。
4. 与 `tb_lfsr_pkg.vhd` 第 33 行的期望值 `"1000000001101"` 比对。

**需要观察的现象 / 预期结果**：你写出的应当是 `bit13=1`、中间 `bit12..5=0`、`bit4=1`、`bit3=1`、`bit2=0`、`bit1=1`，拼起来正好是 `"1000000001101"`，与测试台一致。

**运行验证（待本地验证）**：按 u1-l3 装好 VUnit 后，跑 `lfsr` 库的 `tb_lfsr_pkg.test_get_taps_for_a_few_lengths`，应当全绿（`check_equal` 通过即说明你的手工翻译与代码一致）。

#### 4.1.5 小练习与答案

**练习 1**：位宽 16 的表项是 `(15,13,4,0,0)`，写出对应的 16 位反馈掩码。

**答案**：bit16 恒 1，加 bit15、bit13、bit4。即 `"1001000000001001"`（16 downto 1：bit16=1, bit15=1, bit14=0, bit13=1, bit12..5=0, bit4=1, bit3..1=0）。

**练习 2**：为什么全零种子是非法的？

**答案**：全零状态下，任何抽头位置的异或结果都是 0，反馈永远喂 0 进来，寄存器将永远停在全零、序列不前进，所以必须排除。

**练习 3**：`get_required_lfsr_length` 为什么要求「最低抽头 ≥ shift_count」？

**答案**：多步移位时，反馈区里每个新比特是若干抽头的异或；若某个抽头位置 < shift_count，那它本身也在反馈区里、需要被递推展开，会让异或方程变复杂。强制最低抽头 ≥ shift_count，保证反馈方程简单且可综合。

---

### 4.2 Fibonacci LFSR 的实现：single 与 multi

#### 4.2.1 概念说明

`lfsr_pkg` 只给「字典」，真正产生序列的是两个实体：

- **`lfsr_fibonacci_single`**：每拍移 **1 位**，输出 **1 个比特**。适合做串行伪随机比特流、扰码器。因为只有最高位被当作输出、反馈区只有最低一位，其余位是纯移位，所以综合工具能把长长的移位寄存器折叠进 **SRL（Shift Register LUT）**，资源极省。
- **`lfsr_fibonacci_multi`**：每拍移 **`output_width` 位**，输出 **`output_width` 个比特**。连续两个输出字之间不会有强相关（因为状态已经往前跳了好几步），适合做并行伪随机数。但因为有多个状态位被同时抽头，**一般不能再映射到 SRL**，FF 占用会随位宽上升。

关键设计：`lfsr_fibonacci_single` 其实只是 `lfsr_fibonacci_multi` 在 `output_width=1` 时的薄封装——核心算法只写一遍，避免重复。两者都用 Fibonacci 结构。

#### 4.2.2 核心流程

「每拍移 `shift_count` 位」如何实现？以位宽 15、多项式 \(x^{15}+x^{14}+1\)（抽头 15、14）为例（这正是源码头注释里展开的例子）：

- **移 1 次**：高位整体右移，最低位新值 = `state[15] XOR state[14]`，输出取 `state[15]`。
- **移 2 次**：每个位变成它上方 2 格的值，而进入「反馈区」的新值需要把反馈再递推一次，例如 `state[2] = state[15] XOR state[14]`、`state[1] = state[14] XOR state[13]`，输出取 `state[15:14]`。
- **移 3 次**：同理，反馈区更深，输出取 `state[15:13]`。

通用规律——把状态看成一条线，移 `shift_count` 次后：

```
对每个位 state_idx：
  若 state_idx > shift_count：        # 还在「纯移位区」
      new(state_idx) = state(state_idx - shift_count)
  否则：                              # 进入「反馈区」
      tap_offset = shift_count - state_idx
      new(state_idx) = 把每个抽头 t 的 state(t - tap_offset) 异或起来
```

输出永远是状态的最高 `output_width` 位（Fibonacci 结构里高位是「最新」的位）。代码里 `get_required_lfsr_length` 保证最低抽头 ≥ `shift_count`，所以「反馈区」里的抽头 `t - tap_offset` 总是落在合法、已是已知值的范围内。

#### 4.2.3 源码精读

先看 `lfsr_fibonacci_single`——它非常短，就是一个对 `multi` 的实例化：[modules/lfsr/src/lfsr_fibonacci_single.vhd:24-57](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_fibonacci_single.vhd#L24-L57)

```vhdl
entity lfsr_fibonacci_single is
  generic (
    lfsr_length : positive range non_zero_tap_table'range;
    seed : std_ulogic_vector(lfsr_length downto 1) := (others => '1')
  );
  port(
    clk : in std_ulogic;
    enable : in std_ulogic := '1';
    output : out std_ulogic := '0'
  );
end entity;

architecture a of lfsr_fibonacci_single is
begin
  lfsr_fibonacci_multi_inst : entity work.lfsr_fibonacci_multi
    generic map (
      output_width => 1,
      minimum_lfsr_length => lfsr_length,
      seed => seed
    )
    port map (
      clk => clk,
      enable => enable,
      output(0) => output
    );
end architecture;
```

注意两点：`enable` 默认 `'1'`（不接也每拍跑）；`seed` 默认全 1（合法的非零种子，开箱即用）。

真正干活的在 `lfsr_fibonacci_multi`。先看几处关键声明：[modules/lfsr/src/lfsr_fibonacci_multi.vhd:140-167](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/src/lfsr_fibonacci_multi.vhd#L140-L167)

```vhdl
constant shift_count : positive := output_width;
constant lfsr_length : positive := seed'length;

signal state : std_ulogic_vector(lfsr_length downto 1) := seed;
-- 鼓励 Vivado 推断 SRL
attribute shreg_extract of state : signal is "yes";

-- 把隐含的最高位补进抽头表，让下面的状态计算更简洁
constant taps : natural_vec_t(0 to 5) := (
  0=>lfsr_length, 1 to 5 => non_zero_tap_table(lfsr_length)
);
...
output <= state(state'high downto state'high - output'length + 1);
```

`taps` 这个常量很巧妙：它把「恒参与反馈的最高位」（`taps(0)=lfsr_length`）和表里 5 个非零抽头拼成一个 6 元数组，于是下面的循环只需遍历 `taps` 而不用特判最高位。`output` 直接取状态最高 `output_width` 位。

种子的合法性断言：[modules/lfsr/src/lfsr_fibonacci_multi.vhd:161](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr_fibonacci_multi.vhd#L161)

```vhdl
assert u_unsigned(seed) /= 0 report "Seed all zeros is an invalid state" severity failure;
```

这就是 4.1.1 里说的「全零禁区」的代码守护。

核心递推主进程：[modules/lfsr/src/lfsr_fibonacci_multi.vhd:181-209](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr_fibonacci_multi.vhd#L181-L209)

```vhdl
main : process
  variable next_state : std_ulogic := '0';
  variable tap_offset : natural := 0;
begin
  wait until rising_edge(clk);

  for state_idx in state'range loop
    if state_idx > shift_count then
      next_state := state(state_idx - shift_count);   -- 纯移位区
    else
      tap_offset := shift_count - state_idx;
      next_state := '0';
      for tap_idx in taps'range loop
        if taps(tap_idx) /= 0 then
          -- 注释说明 XOR 与 XNOR 都能用，二者仿真结果相近
          next_state := next_state xnor state(taps(tap_idx) - tap_offset);
        end if;
      end loop;
    end if;

    if enable then
      state(state_idx) <= next_state;
    end if;
  end loop;
end process;
```

把这段对照 4.2.2 的伪代码看，结构完全一致。注意它用的是 `xnor`（同或）而非 `xor`——源码注释说明 Wikipedia 用 XOR、Xilinx 应用笔记用 XNOR，「两者都被仿真验证可用，结果相近」。因为最大长度性质只取决于「反馈抽头集合」，XOR/XNOR 的差别相当于种子集合做了一个取反映射，不影响周期。

资源数字（来自 netlist 回归，见 [modules/lfsr/module_lfsr.py:223-306](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/module_lfsr.py#L223-L306)）把上面「能不能用 SRL」讲得很直白：

| 实体 | 配置 | LUT | FF | 逻辑级数 |
| --- | --- | --- | --- | --- |
| `lfsr_fibonacci_single` | `lfsr_length=52` | 4 | **2** | 2 |
| `lfsr_fibonacci_single` | `lfsr_length=15` | 2 | 2 | 2 |
| `lfsr_fibonacci_multi` | `output_width=12`（实为 13 位 LFSR） | 8 | **13** | 2 |
| `lfsr_fibonacci_multi` | `output_width=16`（实为 19 位 LFSR） | 10 | 19 | 2 |

52 位移位寄存器只用 **2 个 FF**——其余 50 位都折叠进了 SRL（LUT 里的移位寄存器）；而多比特版本 FF 数正好等于 LFSR 位宽（13、19），印证了「multi 不能用 SRL、全部用 FF」。

#### 4.2.4 代码实践

**目标**：实例化 `lfsr_fibonacci_single`，采集一段输出，验证它的周期确实等于 \(2^{n}-1\)。这正是 `tb_lfsr.vhd` 在做的事，我们顺着它的思路走一遍。

**操作步骤**：

1. 读 [modules/lfsr/test/tb_lfsr.vhd:70-109](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/test/tb_lfsr.vhd#L70-L109)，注意两个常量：
   ```vhdl
   constant num_unique_lfsr_states : positive := 2 ** calculated_lfsr_length - 1;
   constant num_samples : positive := num_unique_lfsr_states;
   ```
   即采样数精确等于 \(2^{n}-1\)，随后把每个输出落盘为整数：
   ```vhdl
   for sample_idx in 0 to num_samples - 1 loop
     wait until rising_edge(clk);
     write(f=>file_handle, value=>to_integer(u_unsigned(output)));
   end loop;
   ```
2. 在自己搭的最小 testbench 里，按 `output_width=1`、`desired_lfsr_length` 取一个小值（如 5）实例化 `lfsr_fibonacci_single`；采 \(2^{5}-1=31\) 拍输出，存成一个比特串。
3. 检查：这 31 个值是否两两不同、且第 32 拍是否回到第 1 拍的值。

**需要观察的现象 / 预期结果**：

- 对 `lfsr_length=5`，连续 31 拍输出构成一个周期；第 32 拍应等于第 1 拍。
- 改一个抽头位置（例如把表里 `5 => (3,...)` 改成 `5 => (2,...)`），周期会显著缩短、不再等于 31——这正是项目用频谱后检查要抓住的退化。

**更强的验证（待本地验证）**：项目的 Python 后检查（[module_lfsr.py:95-156](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/lfsr/module_lfsr.py#L95-L156)）对采样做 FFT，期望「只有 DC 分量 + 平坦噪声底」，并由噪声底换算 ENOB，断言它落在 `lfsr_length/2` 附近。换错一个抽头会让这个频谱测试「 spectacularly fail」（注释原话）。这是比「数周期」强得多的最大长度验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `lfsr_fibonacci_single` 的 52 位版本只占 2 个 FF，而 `lfsr_fibonacci_multi` 的 13 位版本却占 13 个 FF？

**答案**：single 每拍只移 1 位、只有最低位在反馈区，长长的移位链可被综合进 SRL（LUT），只有少量状态位需要真正的 FF；multi 每拍移多位、多个状态位被同时抽头，SRL 推断被打断，整条状态必须用 FF 实现，所以 FF 数 ≈ LFSR 位宽。

**练习 2**：代码用 `xnor` 而不是 `xor` 会改变最大长度性质吗？

**答案**：不会。最大长度只由「抽头集合」（本原多项式）决定，XOR 与 XNOR 的差别相当于对状态空间做了一次取反映射，周期仍是 \(2^{n}-1\)。源码注释也确认两者仿真结果相近。

**练习 3**：`enable='0'` 时 LFSR 行为如何？

**答案**：主进程里 `if enable then state(state_idx) <= next_state;` ——`enable='0'` 时所有位保持原值，状态冻结、序列暂停；`enable` 恢复后从原状态继续。注意 `next_state` 仍每拍计算，只是不写入。

---

### 4.3 环形缓冲写端：ring_buffer_write_simple

#### 4.3.1 概念说明

很多 FPGA 应用是「FPGA 高速往一块内存里写、CPU 有空再读走」：DMA 写 DDR、视频帧采集、网络报文缓冲都是这个模式。`ring_buffer_write_simple` 管的就是这块内存的地址——它是一个**写端地址管理器**：

- CPU 先给一段固定的缓冲范围 `[buffer_start_address, buffer_end_address]`，并把 `buffer_read_address` 初始指向起点（表示「我还没读走任何数据」）。
- 使能后，本实体在 `segment` 接口（AXI-Stream 式握手）上**一段一段地发地址**给用户（用户拿到地址就去写那段内存）。
- 用户写完一段后拉一拍 `write_done`，实体据此推进 `buffer_written_address`（告诉 CPU「我写到这儿了」）。
- CPU 把 `buffer_read_address` 往前推（告诉 FPGA「我读到这儿了」），腾出的段就能再次被发出来。

地址发到 `buffer_end_address` 后**回绕（wrap）**到 `buffer_start_address`，像传送带首尾相接，所以叫环形缓冲。

「simple」的含义（见头注释）：每段长度 `segment_length_bytes` 在**编译期固定**，所以地址计算很省。它还有一个和 FIFO 完全同构的关键设计——**永远空一格**来区分「满」与「空」（详见 4.3.2）。

#### 4.3.2 核心流程

地址管理的三个关键技巧：

**① 丢掉必然为 0 的低位（地址对齐）**
段长是 2 的幂，所以地址的低 `ceil_log2(segment_length_bytes)` 位恒为 0（段内偏移由别处管）。实体只保留高位参与索引，省资源：

```
unaligned_segment_address_width = ceil_log2(segment_length_bytes)   # 被丢弃的低位宽
aligned_segment_address_width   = address_width - 上面那个
```

**② 两个游标 + 回绕**
实体维护一个写指针 `buffer_written_index`（已写到哪里）和一个发段指针 `segment_index`（下一个要发的段）。两者走到 `buffer_end_index` 就回绕到 `buffer_start_index`：

```
segment_index_next = (segment_index + 1 == end) ? start : segment_index + 1
```

**③ 永远空一格（满空判定）**
为了让「满」和「空」可区分（两者都是读写指针相等），实体**永不发那个还没被 CPU 读走的段**：

```
最多可 outstanding 的段数 = (end - start) / segment_length_bytes - 1
```

判定逻辑是「下一个要发的段 ≠ 读指针」才发：

```
if segment_index_next != buffer_read_index（对齐后）:
    segment_valid <= '1'   # 还有空段，发出去
```

源码头注释明确写了这条规则：[modules/ring_buffer/src/ring_buffer_write_simple.vhd:41-47](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple.vhd#L41-L47)。这与 u4-l1 同步 FIFO 用「多一位 MSB 区分满空」是同一类思想，只是这里换成了「牺牲一格」。

一个进阶用法（`segments_per_packet > 1`）：把多个段打包成一个「包」，`write_done` 只在包的最后一段拉高，`buffer_written_address` 一次推进 `segments_per_packet * segment_length_bytes`——适合「一个包拆成多次突发写、但只想在包结束时才通知软件」的场景。

#### 4.3.3 源码精读

先看状态 record：[modules/ring_buffer/src/ring_buffer_write_simple_pkg.vhd:18-39](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple_pkg.vhd#L18-L39)

```vhdl
type ring_buffer_write_simple_status_t is record
  idle : std_ulogic;
  start_address_unaligned : std_ulogic;
  end_address_unaligned : std_ulogic;
  read_address_unaligned : std_ulogic;
end record;

constant ring_buffer_write_simple_status_idle_no_error : ... := (idle=>'1', 其余=>'0');
constant ring_buffer_write_simple_status_busy_no_error : ... := (idle=>'0', 其余=>'0');
```

`idle` 表示实体还没使能（测试台靠它判断「已使能、进入忙态」的时机）；三个 `*_unaligned` 是地址没对齐的错误标志。

实体端口与对齐常量：[modules/ring_buffer/src/ring_buffer_write_simple.vhd:94-167](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple.vhd#L94-L167)。注意 `segment` 接口是标准的 `segment_ready`/`segment_valid`/`segment_address` 三件套（AXI-Stream 式）；`write_done` 是用户回写的「这段写完了」脉冲。地址宽度被拆成「对齐段」与「对齐包」两套，是因为段索引粒度可能比包索引细：

```vhdl
constant unaligned_segment_address_width : natural := ceil_log2(segment_length_bytes);
constant aligned_segment_address_width   : natural := address_width - unaligned_segment_address_width;

constant packet_length_bytes            : positive := segments_per_packet * segment_length_bytes;
constant unaligned_packet_address_width : natural := ceil_log2(packet_length_bytes);
```

三条编译期断言把设计前提钉死：[modules/ring_buffer/src/ring_buffer_write_simple.vhd:172-182](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple.vhd#L172-L182)

```vhdl
assert is_power_of_two(segment_length_bytes) report "Must be power of two ..." severity failure;
assert is_power_of_two(segments_per_packet)  report "Must be power of two ..." severity failure;
assert address_width > unaligned_packet_address_width + 1
  report "Buffer must be able to hold at least two packets" severity failure;
```

段长和「每包段数」都必须是 2 的幂（这样取对齐低位只是切片、回绕只是比较，不需要通用除法/取模）；缓冲至少能放两个包（否则「永远空一格」后无段可发）。这又是项目「用 assert 固化设计前提」的典型。

核心主进程（地址递推 + 满空判定 + 握手状态机）：[modules/ring_buffer/src/ring_buffer_write_simple.vhd:231-281](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple.vhd#L231-L281)

```vhdl
main : process
  variable written_index_next, segment_index_next : ... ;
begin
  wait until rising_edge(clk);

  -- 两个游标的「回绕式 +1」
  if segment_index + 1 = buffer_end_index & padding then
    segment_index_next := buffer_start_index & padding;   -- 回绕
  else
    segment_index_next := segment_index + 1;
  end if;

  if buffer_written_index + 1 = buffer_end_index then
    written_index_next := buffer_start_index;              -- 回绕
  else
    written_index_next := buffer_written_index + 1;
  end if;

  -- 使能沿：初始化两个指针到起点
  if enable and not enable_p1 then
    buffer_written_index <= buffer_start_index;
    segment_index <= buffer_start_index & padding;
  end if;

  -- 写完一段：推进已写指针
  if write_done then
    buffer_written_index <= written_index_next;
  end if;

  -- 满空判定 + 握手状态机
  case state is
    when idle =>
      if (enable = '1'
          and segment_index_next /= buffer_read_index & padding) then
        segment_valid <= '1';          -- 下一段还没被读走 → 可发
        state <= wait_for_handshake;
      end if;
    when wait_for_handshake =>
      if segment_ready then            -- 握手成功
        segment_valid <= '0';
        state <= idle;
      end if;
  end case;

  if segment_ready and segment_valid then
    segment_index <= segment_index_next;   -- 发出一段，推进发段指针
  end if;

  enable_p1 <= enable;
end process;
```

注意满空判定就在 `when idle =>` 里：只有当「下一个要发的段 ≠ 读指针」时才拉 `segment_valid`，这就实现了「永远空一格」。`& padding` 是因为包索引粒度比段索引粗，比较时要把包索引低位补零对齐。

`status.idle <= to_sl(state = idle);` 把状态机的 idle 翻译成 status 位（[第 283 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple.vhd#L283)）。对齐错误检测只在 `packet_length_bytes > 1` 时才生成（[第 287-310 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/src/ring_buffer_write_simple.vhd#L287-L310)）——单字节段时无所谓对齐，省一块逻辑（generic 裁剪、零资源哲学的体现）。

资源回归（[module_ring_buffer.py:56-71](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/module_ring_buffer.py#L56-L71)）：`address_width=29, segment_length_bytes=64` 时为 **94 LUT / 52 FF / 逻辑级数 12**。

#### 4.3.4 代码实践

**目标**：在固定深度缓冲上连续写段，观察写指针（`buffer_written_address` / 发段地址）在到达 `buffer_end_address` 时回绕到起点。

**操作步骤**：

1. 打开 [modules/ring_buffer/test/tb_ring_buffer_write_simple.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/test/tb_ring_buffer_write_simple.vhd)，重点看 `check_segment` 进程（[第 162-176 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/test/tb_ring_buffer_write_simple.vhd#L162-L176)）：
   ```vhdl
   expected := to_integer(buffer_start_address)
             + (num_served mod buffer_size_segments) * segment_length_bytes;
   check_equal(segment_address, expected, "num_served: " & to_string(num_served));
   ```
   它断言「第 k 次发的段地址 = 起点 + (k mod 段数) × 段长」——这正是回绕的数学表达。
2. 看 `check_within_range` 进程（[第 197-207 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/test/tb_ring_buffer_write_simple.vhd#L197-L207)）：
   ```vhdl
   check_relation(num_served < num_processed + buffer_size_segments, ...);
   ```
   它保证 outstanding 段数 < 总段数，即「永远空一格」始终成立。
3. 想自己观察时：搭一个最小 testbench，设 `buffer_start_address=0`、`buffer_end_address=4×segment_length_bytes`（共 4 段），`buffer_read_address` 始终滞后追赶；连续应答 `segment_valid` 并每段回 `write_done`，把 `segment_address` 逐拍打印。

**需要观察的现象 / 预期结果**：

- 发出的段地址序列形如 `0, S, 2S, 3S, 0, S, 2S, 3S, 0, ...`（S = `segment_length_bytes`），到末段后回绕回 0。
- 若故意让 `buffer_read_address` 停下不推进，当 outstanding 达到「段数 − 1」时，`segment_valid` 会停拉高——实体拒绝覆盖 CPU 还没读走的那一格。
- `test_invalid_addresses` 用例里，把 `buffer_start_address` 设成未对齐值（如 3），`status.start_address_unaligned` 会在下一拍置 1。

**运行验证（待本地验证）**：`module_ring_buffer.py` 对 `test_random_addresses` 用 `segment_length_bytes ∈ {1,4,8}` × `buffer_size_segments ∈ {2,4,16}` 组成 9 组配置（[第 30-39 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/ring_buffer/module_ring_buffer.py#L30-L39)），跑这些配置应全绿。

#### 4.3.5 小练习与答案

**练习 1**：为什么缓冲「必须能放下至少两个段/包」？

**答案**：因为实体永远保留一格不发（用来区分满与空）。若整个缓冲只有 1 个段，扣掉保留的那一格后可 outstanding 的段数 = 0，永远无段可发，所以至少要 2 个。

**练习 2**：`segment_length_bytes` 为什么必须是 2 的幂？

**答案**：这样段内偏移占的低位宽 = `ceil_log2(segment_length_bytes)` 是整数，对齐只需把地址的低位切片丢掉、回绕只需比较高位，无需通用除法/取模，资源省、时序好。断言用 `is_power_of_two` 强制。

**练习 3**：`segments_per_packet > 1` 时，`buffer_written_address` 何时、推进多少？

**答案**：只在「包的最后一段」的 `write_done` 拉高时推进，一次推进 `segments_per_packet × segment_length_bytes`，让软件看到一个完整的包边界而不是每个小段都被告知。

---

## 5. 综合实践

把本讲两个模块串起来，设计一个「LFSR 驱动随机写节奏的环形缓冲」迷你场景，验证两件事：LFSR 的周期性与环形缓冲的回绕。

**任务**：

1. 实例化一个 `lfsr_fibonacci_single`（取 `lfsr_length=7`，周期 \(=127\)）作为伪随机源。
2. 实例化一个 `ring_buffer_write_simple`（`segment_length_bytes=4`，缓冲 8 段，`buffer_start_address=0`，`buffer_end_address=32`）。
3. 把 LFSR 的输出比特当作「是否在本拍应答 `segment_ready` / 是否推进读指针」的随机控制（参考 `tb_ring_buffer_write_simple.vhd` 用 `rnd.Uniform(0,20)` 制造随机延迟的写法，只是把随机源换成 LFSR，从而完全可复现）。
4. 运行足够久，让写指针回绕若干圈。

**需要观察 / 验证的点**：

- LFSR 侧：单比特输出在 127 拍后回到初值（可单独采一段验证）。
- 环形缓冲侧：`segment_address` 序列在 `0,4,8,...,28` 后回绕到 `0`；任意时刻 outstanding 段数不超过 7（8 − 1，永远空一格）。
- 把 LFSR 当随机源的好处：序列确定性可复现，出问题时每次仿真都能复现同一现象——这正是 `tb_lfsr.vhd` 用 `get_string_seed(runner_cfg)` 初始化 RNG 的同一动机。

**预期结果（待本地验证）**：两个断言（LFSR 周期 = 127、段地址按 `(num_served mod 8)*4` 递推）都能持续成立；写指针在缓冲末尾干净回绕、从不越过读指针。

> 进阶：把 `lfsr_fibonacci_single` 换成 `lfsr_fibonacci_multi`（`output_width=4`），用 4 比特随机数控制延迟，注意此时 LFSR 实际位宽会被 `get_required_lfsr_length` 自动选到 ≥ 5，资源会从「SRL 为主」变成「FF 为主」（对照 4.2.3 的资源表）。

## 6. 本讲小结

- 最大长度 LFSR 用「移位 + 抽头异或」产生周期为 \(2^{n}-1\) 的伪随机序列，全零状态是必须排除的锁死态。
- `lfsr_pkg` 把 2–64 位的本原抽头存成表，`get_lfsr_taps` 翻译成反馈掩码、`get_required_lfsr_length` 为多步移位挑出合适的位宽；两者都是精化期函数、不占电路资源。
- `lfsr_fibonacci_single` 是 `lfsr_fibonacci_multi` 在 `output_width=1` 的薄封装；single 能映射到 SRL（52 位仅 2 个 FF），multi 因多比特抽头不能映射 SRL（FF 数 ≈ 位宽）。
- 多步移位的核心是「纯移位区直接取值、反馈区按 `tap_offset` 异或各抽头」；`xnor` 与 `xor` 等价，不影响最大长度。
- `ring_buffer_write_simple` 是 FPGA 写、CPU 读的环形地址管理器：丢掉对齐低位、两游标回绕、握手发段、`write_done` 推进写指针。
- 与 FIFO 同构的关键设计是「永远空一格」区分满与空，所以最多 outstanding 段数 = 总段数 − 1；段长、每包段数必须是 2 的幂，缓冲至少放两个包。
- 两者的正确性都不是靠「眼看」，而是靠测试台 + Python 后检查（LFSR 用 FFT 频谱验最大长度、环形缓冲用 `num_served < num_processed + size` 验空一格）与 netlist 资源回归（`EqualTo` 锁死 LUT/FF）双重保证。

## 7. 下一步学习建议

- **u7-l2 DMA 架构**：`ring_buffer_write_simple` 正是 `dma_axi_write_simple` 把 AXI-Stream 数据流写进 DDR 的地址引擎，读完本讲再去看 DMA 会非常顺。
- **u8-l1 BFM**：本讲的环形缓冲测试台用了 `bfm.handshake_slave` 施加随机背压，可以系统学一下 BFM 如何驱动握手验证。
- **u8-l3 资源占用回归**：本讲反复引用的 `TotalLuts(EqualTo(...))`、`MaximumLogicLevel(...)` 来自 netlist 构建回归，建议学完后再回看 `module_lfsr.py` / `module_ring_buffer.py` 的 `get_build_projects`。
- **延伸阅读**（源码头注释里给出的权威资料）：LFSR 数学背景见 Wikipedia「Linear-feedback shift register」「Maximum length sequence」与 Xilinx 应用笔记 [xapp210](https://docs.xilinx.com/v/u/en-US/xapp210)。
