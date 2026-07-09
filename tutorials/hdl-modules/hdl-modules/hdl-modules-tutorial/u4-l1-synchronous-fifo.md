# 同步 FIFO：多功能与面积优化

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `fifo` 实体两侧的 ready/valid 握手接口，以及 `level`、`almost_full`、`almost_empty` 三个状态信号的语义。
- 解释读写指针如何用「多一位」的技巧区分「满」和「空」，并写出 `level` 的计算式。
- 理解 `enable_last`、`enable_packet_mode`、`enable_drop_packet`、`enable_peek_mode` 四个 generic 各自打开了什么功能，以及它们之间层层依赖的关系。
- 说明 `enable_output_register` 如何复用 u2-l1 的 `handshake_pipeline`，并把寄存器「吸收」进 BRAM 的硬输出寄存器。
- 用项目自带的 netlist 资源回归数据，量化「每多开一个 generic 要付出多少 LUT/FF」。

本讲是 FIFO 系列的第一篇，只讲**单时钟（同步）**FIFO；双时钟异步 FIFO 留给 u4-l2。

## 2. 前置知识

本讲假定你已经读过：

- **u2-l1**：项目的 AXI-Stream 式 ready/valid 握手约定（`valid` 不得组合依赖 `ready`；`ready` 可组合依赖 `valid`；`valid and ready` 同拍为 1 才算一次 beat；若干 beat 由 `last` 结尾构成 packet）。本讲的 FIFO 接口完全遵循这套约定。
- **u2-l2**：`common` 模块的三个基础包。其中 `types_pkg` 的 `to_int`/`to_sl`、`attribute_pkg` 的 `ram_style_t` 枚举与 `to_attribute` 转换函数会在 FIFO 里直接用到。

再补两个本讲要用到的名词：

- **FIFO（先进先出缓冲）**：一个「写入端把数据按顺序塞进去、读取端按相同顺序取出来」的弹性缓冲。写快读慢时它吸收突发，读快写慢时它平滑数据流。
- **满（full）/空（empty）**：满表示写端必须停（`write_ready='0'`），空表示读端没有数据可读（`read_valid='0'`）。
- **关键路径 / 逻辑级数（logic level）**：组合逻辑从输入到输出穿过的逻辑门层数；层数越高，时序越难收敛。`module_fifo.py` 里的 `MaximumLogicLevel` 检查器正是在回归这个值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [modules/fifo/src/fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd) | 同步 FIFO 的可综合实体，本讲的主角。 |
| [modules/fifo/doc/fifo.rst](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/doc/fifo.rst) | 模块文档入口（指向网站）。 |
| [modules/fifo/test/tb_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd) | 同步 FIFO 的 VUnit 测试台，用 BFM 驱动随机化握手验证。 |
| [modules/fifo/module_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py) | 模块的 Python 配置：`setup_vunit` 枚举仿真 generic 矩阵，`get_build_projects` 定义 netlist 资源回归。 |
| [modules/fifo/rtl/fifo_netlist_build_wrapper.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/rtl/fifo_netlist_build_wrapper.vhd) | 只引出「裸接口」的顶层封装夹具，供 netlist 构建做最小化面积断言。 |

按 u1-l2 的目录约定：`src/` 进综合工程与仿真工程；`test/` 只进仿真工程；`rtl/` 是 netlist 构建用的封装夹具。本讲主要精读 `src/fifo.vhd`。

## 4. 核心概念与源码讲解

本讲把同步 FIFO 拆成四个最小模块：①接口与状态信号，②存储核心与满/空判定，③四个功能 generic，④输出寄存器与面积优化。

### 4.1 接口全景：ready/valid 握手与状态信号

#### 4.1.1 概念说明

FIFO 的对外接口就是两套 u2-l1 教过的 AXI-Stream 式握手：

- **写侧**：`write_valid`（主→FIFO，数据有效）、`write_ready`（FIFO→主，还能再收）、`write_data`（负载）、可选 `write_last`（包末拍）。`write_ready='0'` 即 FIFO **满**。
- **读侧**：`read_valid`（FIFO→主，有数据可读）、`read_ready`（主→FIFO，可以收）、`read_data`、可选 `read_last`。`read_valid='0'` 即 FIFO **空**。

围绕满/空，实体还提供三个状态信号：

- `level`：FIFO 里当前有多少个 word。
- `almost_full`：当 `level >= almost_full_level` 时拉高，让上游「快满了」提前减速。
- `almost_empty`：当 `level <= almost_empty_level` 时拉高，让下游「快空了」提前准备。

#### 4.1.2 核心流程

```
       写侧握手                  读侧握手
write_valid ─┐               ┌─ read_valid
write_ready ←┤   ┌──────┐    ├→ read_ready
write_data  ──┴──►│ FIFO │────├─ read_data
write_last  ─────►│ RAM  │────├─ read_last
                   └──┬───┘    │
                      │        │
                 level, almost_full, almost_empty
```

一次 beat 完成的条件仍是 u2-l1 的规则：`write_ready and write_valid` 同拍为 1，写走一个 word；`read_ready and read_valid` 同拍为 1，读走一个 word。

#### 4.1.3 源码精读

实体声明在 [modules/fifo/src/fifo.vhd:65-127](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L65-L127)，generic 区列出了 `width`、`depth` 与全部功能开关，port 区按写侧 / 读侧 / 状态分块。注意几个默认值的设计意图：

- `write_ready` 初值 `'1'`、`read_valid` 初值 `'0'`、`almost_empty` 初值 `'1'`（[L103-L125](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L103-L125)）——上电即「不满、空」，符合空 FIFO 的物理事实。
- `write_last`、`drop_packet`、`read_peek_mode` 默认 `'0'`（[L107](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L107)、[L112](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L112)、[L123](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L123)）：不开对应 generic 时这些端口悬空即可，不会被综合成悬空线。

`almost_full`/`almost_empty` 的生成用了一个省资源的技巧：当阈值取默认极值（`almost_full_level = depth` 或 `almost_empty_level = 0`）时，直接复用现成信号，不做比较器：

```vhdl
assign_almost_full : if almost_full_level = depth generate
  almost_full <= not write_ready;            -- 满 = 几乎满
else generate
  almost_full <= to_sl(level > almost_full_level - 1);
end generate;
```

见 [modules/fifo/src/fifo.vhd:201-213](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L201-L213)。源码注释也提醒：非默认阈值会「增加逻辑占用」——这正是后面 netlist 回归里 `with_levels` 比 `minimal` 多出十几个 LUT 的原因。

#### 4.1.4 代码实践

**目标**：观察空 FIFO 的初始接口状态。

**步骤**：

1. 打开 [modules/fifo/test/tb_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd)，找到 `test_init_state` 这个用例（[L146-L159](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L146-L159)）。
2. 按 u1-l3/u1-l4 的方式配置好 PYTHONPATH 与 VUnit，运行：
   ```
   python tools/simulate.py fifo --test "*test_init_state*"
   ```
3. 阅读该用例的四条 `check_equal` 断言。

**需要观察的现象**：仿真开始时 `read_valid='0'`、`write_ready='1'`、`almost_full='0'`、`almost_empty='1'`，且空闲 1 µs 后仍保持不变。

**预期结果**：四条断言全部通过，证明空 FIFO 的接口电平与 4.1.3 推导一致。**待本地验证**（取决于本机是否装好 VUnit/Vivado 仿真器）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `write_ready` 的初值是 `'1'` 而不是 `'0'`？

**答案**：空 FIFO 当然没满，应当能立即接收写入；初值 `'1'` 让上电后第一个周期就可以握手，避免无谓的停顿。

**练习 2**：`almost_full_level` 取默认值 `depth` 时，为什么 `almost_full <= not write_ready` 是等价的？

**答案**：`almost_full_level = depth` 表示「FIFO 满才算几乎满」，而满等价于 `write_ready='0'`，故 `almost_full = not write_ready`，省掉一个比较器。

---

### 4.2 存储核心：读写指针与满/空判定

#### 4.2.1 概念说明

FIFO 的存储是一个环形 RAM：写指针 `write_addr` 指向下一个写入位置，读指针 `read_addr` 指向下一个读出位置，两者都在 `0 .. memory_depth-1` 之间循环。核心难题是：**当两个指针相等时，到底是「空」还是「满」？** 它们长相完全一样。

经典解法是给指针**多留一位**最高位（MSB）：刚复位两指针相等 → 空；写入追上读出、低位再次相等但 MSB 翻过一次 → 满。

#### 4.2.2 核心流程

设真实 RAM 深度为 \(D\)（`memory_depth`），指针位宽为 \(N\)，其中低位 \(n=\lceil\log_2 D\rceil\) 用于寻址 RAM，最高位用来区分满空，故：

\[ N = \text{num\_bits\_needed}(2D - 1) \]

判定规则（同步 FIFO 用二进制指针即可，因为读写同域；异步 FIFO 必须改用格雷码，那是 u4-l2 的事）：

\[ \text{满} \iff \text{低位}(write) = \text{低位}(read)\ \text{且}\ \text{MSB}(write) \neq \text{MSB}(read) \]

\[ \text{空} \iff write = read \]

`level`（当前 word 数）则用模运算直接算：

\[ \text{level} = \big((write\_addr\_next - read\_addr\_next)\bmod 2D\big) + \text{word\_in\_output\_register\_next} \]

注意它用的是 `_next`（下一拍值），所以 `level` 永远反映「即将到来的那个周期」的状态——源码注释称之为「always correct」。

#### 4.2.3 源码精读

指针类型定义见 [modules/fifo/src/fifo.vhd:131-141](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L131-L141)：

```vhdl
constant memory_depth : positive := depth - to_int(enable_output_register);
-- 多一位用来区分满/空
subtype fifo_addr_t is u_unsigned(num_bits_needed(2 * memory_depth - 1) - 1 downto 0);
-- 真正送进 BRAM 地址端口的只有低位
subtype bram_addr_range is natural range num_bits_needed(memory_depth - 1) - 1 downto 0;
```

这里的 `num_bits_needed`、`is_power_of_two` 都来自 `math_pkg`（[math_pkg.vhd:65](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L65)、[L221](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L221)）。

满判定被直接写进 `write_ready`（[modules/fifo/src/fifo.vhd:285-287](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L285-L287)）：

```vhdl
write_ready <= to_sl(
  read_addr(bram_addr_range) /= write_addr_next_if_not_drop(bram_addr_range)   -- 低位不等
  or read_addr(read_addr'high) = write_addr_next_if_not_drop(write_addr_next'high));  -- 或 MSB 相等
```

把 4.2.2 的「满」取反就是「不满 = `write_ready='1'`」：低位不等 **或** MSB 相等。注释（[L274-L284](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L274-L284)）还解释了一个刻意的时序取舍：`write_ready` 故意看「未 drop 的下一指针」和「当前读指针」，以放宽关键路径，代价是「满与 drop 同拍发生」这一罕见情形下 `write_ready` 会多低一拍。

RAM 本体在 `memory_block` 块里（[modules/fifo/src/fifo.vhd:409-443](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L409-L443)），核心是一个时钟进程，同拍做「读出 + 写入」：

```vhdl
memory : process is
begin
  wait until rising_edge(clk);
  memory_read_data <= mem(to_integer(read_addr_next) mod memory_depth);  -- 同步读，1 拍延迟
  if write_ready and write_valid then
    mem(to_integer(write_addr) mod memory_depth) <= memory_write_data;
  end if;
end process;
```

读地址用 `read_addr_next`（下一拍值），所以数据在**下一拍**出现在 `memory_read_data`——这是 FIFO 读侧固有的 1 拍延迟的根源，也是 4.4 节要解决的时序问题。

`level` 的赋值在 [modules/fifo/src/fifo.vhd:331-333](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L331-L333)，与 4.2.2 的公式一一对应。

#### 4.2.4 代码实践

**目标**：体会满/空翻转与 `level` 的变化。

**步骤**：

1. 打开 [tb_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd) 的 `test_write_faster_than_read` 用例（[L161-L167](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L161-L167)）：它把读侧 stall 概率调到 90%，制造「写快读慢」，让 FIFO 反复被写满。
2. 运行：
   ```
   python tools/simulate.py fifo --test "*write_faster_than_read*"
   ```
3. 关注 `status_tracking` 进程（[tb_fifo.vhd:381-402](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L381-L402)）统计的 `has_gone_full_times`。

**需要观察的现象**：FIFO 会反复触达满（`write_ready` 在 `write_valid` 为 1 时被拉低），`has_gone_full_times` 累计超过 500 次。

**预期结果**：末尾 `check_relation(has_gone_full_times > 500)` 通过。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`depth=16` 且不开输出寄存器时，`fifo_addr_t` 的位宽是多少？

**答案**：`memory_depth = 16`，`num_bits_needed(2*16-1) = num_bits_needed(31) = 5`，故指针 5 位（低 4 位寻址 RAM，第 5 位区分满空）。

**练习 2**：为什么 `level` 用 `write_addr_next - read_addr_next` 而不是已寄存的 `write_addr - read_addr`？

**答案**：用 `_next`（下一拍值）让 `level` 在当前周期末就反映刚发生的读写，保持「always correct」；若用已寄存指针会滞后一拍，对依赖 `level` 做决策的上游/下游不够精确。

---

### 4.3 用 generic 开关功能：last / packet_mode / drop_packet / peek_mode

#### 4.3.1 概念说明

这是本 FIFO 最有特色的地方，也是项目「用 generic 裁剪功能以省资源」哲学（见 u1-l1）的集中体现。四个 generic 打开四档能力，且**层层依赖**：

| generic | 打开的能力 | 依赖前提 |
| --- | --- | --- |
| `enable_last` | 把 `write_last` 连同 `write_data` 一起存进 RAM，读出时还原为 `read_last` | 无 |
| `enable_packet_mode` | 读侧必须等到**整包**写完（收到 `write_last`）才允许 `read_valid` 拉高 | 必须 `enable_last` |
| `enable_drop_packet` | `drop_packet` 端口可丢弃「正在写的那一包」，指针回退到包首 | 必须 `enable_packet_mode` |
| `enable_peek_mode` | `read_peek_mode` 有效时读完一包不弹出，可反复读同一包 | 必须 `enable_packet_mode`，且**不能**与 `enable_output_register` 同用 |

不开某个 generic 时，对应 `generate` 块整体消失，零资源占用。

#### 4.3.2 核心流程

**packet_mode 的「整包可见」语义**：维护一个计数器 `num_lasts_in_fifo`，记录 FIFO 里已经存了几个完整的包（即几个 `last` beat）。

```
每写入一个 write_last（且未 drop）     → num_lasts_in_fifo + 1
每读出一个 read_last（且未 peek）       → num_lasts_in_fifo - 1
read_valid_ram_pre <= '1'  当且仅当  num_lasts_in_fifo /= 0
```

于是 `read_valid` 只在「至少有一个完整包」时才拉高——半截包对读侧不可见。

**drop_packet 的「指针回退」语义**：记录每个包起始地址 `write_addr_start_of_packet`。一旦 `drop_packet` 有效，下一写指针直接回退到包首：

```vhdl
write_addr_next <= write_addr_start_of_packet when should_drop_packet
                   else write_addr_next_if_not_drop;
```

正在写的那一包就像从没写过一样被丢弃。

**peek_mode 的「双读指针」语义**：维护两个读地址——`read_addr`（包起始，参与满判定，防止未弹出的数据被覆盖）和 `read_addr_peek`（实际读地址，可前进也可回退）。读到 `read_last` 且处于 peek 模式时，下一地址跳回 `read_addr`，于是整包可以再读一遍。

#### 4.3.3 源码精读

generic 声明与依赖注释见 [modules/fifo/src/fifo.vhd:73-89](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L73-L89)。架构开头用一组 `assert ... severity failure` 把依赖关系固化成编译期/仿真期硬约束（[modules/fifo/src/fifo.vhd:157-175](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L157-L175)）：

```vhdl
assert is_power_of_two(memory_depth)                     -- RAM 深度必须是 2 的幂
assert enable_last or (not enable_packet_mode)           -- 包模式必须先开 last
assert enable_packet_mode or (not enable_drop_packet)    -- drop 必须先开包模式
assert enable_packet_mode or (not enable_peek_mode)      -- peek 必须先开包模式
assert not (enable_output_register and enable_peek_mode) -- peek 与输出寄存器互斥
```

> 一个容易踩的细节：断言检查的是 `memory_depth`（真实 RAM 深度），而 `memory_depth = depth - to_int(enable_output_register)`（[L131](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L131)）。所以开输出寄存器时，必须传 `depth = 2^k + 1`（例如 1025），让 `depth-1 = 1024` 仍是 2 的幂——`module_fifo.py` 里 `depth=generics["depth"]+1` 正是为此。

packet_mode 的计数逻辑在 `status` 进程里（[modules/fifo/src/fifo.vhd:227-267](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L227-L267)）。注意它故意用**流水一级**的 `write_last_transaction_p1` 而不是当拍的 `write_last`：

```vhdl
num_lasts_in_fifo_next := num_lasts_in_fifo
  + to_int(write_last_transaction_p1)                       -- 上一拍写入的 last
  - to_int(read_ready and read_valid and read_last and not should_peek_read);
write_last_transaction_p1 <= write_ready and write_valid and write_last and not should_drop_packet;
```

源码注释（[L232-L236](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L232-L236)）解释：必须等一拍，保证有效写数据有时间穿过 RAM 抵达读侧后，计数器才指示「有包可读」——这对**长度仅 1 拍的包**至关重要。代价是估计偏悲观。

drop 与 peek 的指针选择见 [modules/fifo/src/fifo.vhd:338-365](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L338-L365)：`write_addr_next` 在 drop 时回退到 `write_addr_start_of_packet`；peek 模式用独立进程 `calc_peek_addr` 在读到 `read_last` 时把读地址拉回包首。

`enable_last` 本身则只是在 RAM 字宽里多塞 1 位（[modules/fifo/src/fifo.vhd:410与425-428](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L408-L428)）：`memory_word_width := width + to_int(enable_last)`，`last` 拼在 `data` 高位一起存取。所以 netlist 回归显示「开 `enable_last` 几乎不增加 LUT/FF」。

#### 4.3.4 代码实践（本讲主实践）

**目标**：实例化 `fifo` 并启用 `enable_last` 与 `enable_packet_mode`，验证 `read_valid` 只在收到 `write_last` 后才拉高，且能完整读出整包。

**步骤**：

1. 项目已经把这个验证写好了——打开 [tb_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd)，DUT 例化处把 `enable_packet_mode` 与 `enable_last` 同时打开（[L432-L445](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L432-L445)）。
2. 关注 `test_packet_mode_status` 用例（[L182-L192](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L182-L192)）：它关掉读侧，写入 `depth-1` 个 beat（但还没写 `last`），然后断言 `read_valid` 仍为 `False`。
3. 再看 `test_packet_mode_random_data`（[L177-L180](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L177-L180)）：随机长度包反复写读，由 `axi_stream_slave` BFM 自动核对每包数据与 `last` 标记。
4. 运行仿真：
   ```
   python tools/simulate.py fifo --test "*packet_mode*"
   ```

**需要观察的现象**：
- 在 `test_packet_mode_status` 中，写入了大量数据但未发 `write_last` 时，`read_valid` 始终为 `0`。
- 一旦 `write_last` 到达，下一拍起 `read_valid` 拉高，且整包能被完整读出。

**预期结果**：所有 packet_mode 相关用例通过；BFM 不报数据/`last` 不匹配错误。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果只开 `enable_packet_mode`、忘了开 `enable_last`，会发生什么？

**答案**：编译/仿真时会触发 `assert enable_last or (not enable_packet_mode)` 失败（`severity failure`），设计直接报错停下——这正是把依赖关系做成断言的价值。

**练习 2**：`peek_mode` 为什么不能和 `enable_output_register` 一起用？

**答案**：见 [L173-L175](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L173-L175) 的断言。输出寄存器会改变读数据的时序位置，使 peek 模式「读完一包跳回包首」的地址管理无法与寄存器中的残留 word 协调，故二者互斥。

**练习 3**：`num_lasts_in_fifo` 为什么用 `write_last_transaction_p1`（延迟一拍）而不是当拍的 `write_last`？

**答案**：要让有效数据先穿过 RAM 到达读侧，计数器再声明「有完整包可读」。否则对长度仅 1 拍的包，`read_valid` 可能在数据尚未到达读端口时就拉高，读出错误数据。

---

### 4.4 输出寄存器、ram_type 与面积优化

#### 4.4.1 概念说明

4.2 节提到读侧有 1 拍 RAM 延迟，且 BRAM 输出数据的布线延迟通常较大，容易成为关键路径。`enable_output_register` 在读路径上再插一级寄存器改善时序。妙处在于：这级寄存器**不是普通触发器**，而是被「吸收」进 BRAM 原语自带的「硬输出寄存器」（hard output register），所以几乎不增加 FF。

另一个面积杠杆是 `ram_type`（类型 `ram_style_t`，来自 u2-l2 的 `attribute_pkg`）：它通过 Vivado 的 `ram_style` 综合属性，决定 RAM 用 BRAM、分布式 LUTRAM 还是让工具自选。

#### 4.4.2 核心流程

输出寄存器的插入并非手写一段 `process`，而是**复用 u2-l1 的 `handshake_pipeline`**：

```
                       enable_output_register = false → 纯 route-through（skid buffer，满吞吐）
read_data_ram ──► handshake_pipeline ──► read_data
                       enable_output_register = true  → 插一级数据流水（映射到 BRAM 硬输出寄存器）
```

当 `enable_output_register = true`：

- `memory_depth = depth - 1`：RAM 少深一格，让出的位置由输出寄存器顶上，所以对外的 `depth` 语义不变。
- 额外用一个 `word_in_output_register` 计数（0 或 1）追踪输出寄存器里有没有 word，参与 `level` 计算。

`ram_type` 则只是一行属性：

```vhdl
attribute ram_style of mem : signal is to_attribute(ram_type);
```

`to_attribute(ram_style_t)` 是 u2-l2 讲过的强类型转换，把枚举（`ram_style_auto`/`ram_style_blockram`/`ram_style_distributed`/...）变成综合器认识的字符串，避免手写字符串打错。

#### 4.4.3 源码精读

`handshake_pipeline` 的例化在 [modules/fifo/src/fifo.vhd:447-468](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L447-L468)：

```vhdl
handshake_pipeline : entity common.handshake_pipeline
  generic map (
    data_width => width,
    full_throughput => true,                 -- 满吞吐，不降速
    pipeline_control_signals => false,
    -- 仅当开输出寄存器才真正插一级数据流水，否则 route-through
    pipeline_data_signals => enable_output_register)
  port map ( ... read_ready_ram/read_valid_ram/read_data_ram  ⇒  read_ready/read_valid/read_data ... );
```

这正是 u2-l1 讲过的「满吞吐 + 只流水数据 + 不流水控制」组合，被 FIFO 直接拿来当可选输出级。

`ram_style` 属性声明在 [modules/fifo/src/fifo.vhd:415](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L415)，`word_in_output_register` 的维护在 [modules/fifo/src/fifo.vhd:316-325](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L316-L325)（输入侧握手 +1、输出侧握手 -1）。

**面积证据**——这是本讲最直观的一张表，全部来自 [module_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py) 的 netlist 构建断言（目标器件 `xc7z020clg400-1`，width=32、depth=1024）：

| 构建名 | 开启的特性 | TotalLuts | Ffs | BRAM | 逻辑级数 | 出处 |
| --- | --- | --- | --- | --- | --- | --- |
| `fifo.minimal` | 无 | 14 | 24 | 1×Ramb36 | 6 | [L143-L158](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L143-L158) |
| `fifo.minimal_with_output_register` | + 输出寄存器 | 15 | 25 | 1×Ramb36 | 6 | [L162-L178](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L162-L178) |
| `fifo.with_levels` | + level 口 + 非默认阈值 | 27 | 35 | 1×Ramb36 | 6 | [L182-L198](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L182-L198) |
| `fifo.with_last` | + enable_last | 27 | 35 | 1×Ramb36 | 6 | [L201-L217](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L201-L217) |
| `fifo.with_packet_mode` | + packet_mode | 40 | 47 | 1×Ramb36 | 6 | [L221-L237](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L221-L237) |
| `fifo.with_packet_mode_and_output_register` | + 输出寄存器 | 45 | 50 | 1×Ramb36 | **9** | [L241-L257](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L241-L257) |
| `fifo.with_drop_packet` | + drop_packet | 45 | 58 | 1×Ramb36 | 6 | [L261-L279](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L261-L279) |
| `fifo.with_peek_mode` | + peek_mode | 58 | 58 | 1×Ramb36 | 6 | [L283-L299](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L283-L299) |
| `fifo.lutram_minimal` | 无（width=8, depth=32） | 32 | 22 | **0** | 5 | [L309-L324](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L309-L324) |

读这张表能得到几条工程结论：

- **输出寄存器几乎免费**：`minimal` → `minimal_with_output_register` 只多 1 LUT、1 FF——印证了「寄存器被吸进 BRAM 硬输出寄存器，不吃额外触发器」。
- **enable_last 真免费**：`with_levels` → `with_last` 数字完全不变，因为 `last` 只是 RAM 字宽 +1。
- **packet_mode 要付一个计数器**：+13 LUT、+12 FF。
- **peek_mode 最贵**：+18 LUT（相对 packet_mode），因为多了一个读地址指针和数据 muxing。
- **浅 FIFO 自动走 LUTRAM**：`lutram_minimal`（depth=32）一个 BRAM 都不用，资源用 LUT 实现。
- **输出寄存器 + packet_mode 会抬高逻辑级数到 9**：这是「以时序换寄存器」的代价，源码注释（[L390-L397](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L390-L397)）专门讨论了这个取舍。

#### 4.4.4 代码实践

**目标**：亲手跑一次 netlist 综合，验证「开输出寄存器几乎不增 FF」。

**步骤**：

1. 按 u1-l3 装好 Vivado 与 tsfpga。
2. 用 u9-l2 将详细讲解的 `tools/synthesize.py` 对 `fifo_netlist_build_wrapper` 综合两组配置（**示例命令**，具体参数以 `tools/synthesize.py --help` 为准）：
   ```
   # 组 A：不开输出寄存器
   python tools/synthesize.py fifo_netlist_build_wrapper \
       --generic use_asynchronous_fifo=false --generic width=32 \
       --generic depth=1024 --generic enable_output_register=false

   # 组 B：开输出寄存器（depth 需 +1 以满足 memory_depth 为 2 的幂）
   python tools/synthesize.py fifo_netlist_build_wrapper \
       --generic use_asynchronous_fifo=false --generic width=32 \
       --generic depth=1025 --generic enable_output_register=true
   ```
3. 对比两份资源报告里的 FF 数。

**需要观察的现象**：组 B 的 FF 数应当与组 A 几乎相同（仅多 1 个），证明多出的那一级寄存器被吸收进 BRAM 硬输出寄存器。

**预期结果**：FF 增量约 1，与 4.4.3 表中 `minimal`(24) → `minimal_with_output_register`(25) 一致。**待本地验证**（命令参数以本地 `--help` 为准）。

> 若本机没有 Vivado，可改为**源码阅读型实践**：直接对照表中 `fifo.with_packet_mode_and_output_register` 的逻辑级数 9 与其他行的 6，结合 [fifo.vhd:390-397](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L390-L397) 的注释，解释为何「输出寄存器 + packet_mode」会抬高关键路径逻辑级数。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `enable_output_register=true` 时要把 `depth` 设成 `2^k + 1`？

**答案**：因为断言要求 `memory_depth = depth - 1` 是 2 的幂。`depth = 2^k + 1` 时 `memory_depth = 2^k`，满足约束；多出的一格深度由输出寄存器补上，对外 `depth` 语义不变。

**练习 2**：`fifo.lutram_minimal` 为什么一个 BRAM 都没用？

**答案**：它 `depth=32` 且 `width=8`，容量很小，Vivado 在 `ram_style_auto` 下选择用分布式 LUTRAM 实现，比切块 BRAM 更划算——这正是 `ram_type` 默认让工具自选的好处。

**练习 3**：从面积表看，哪个功能「性价比最差」（开销大）？你会如何在设计中取舍？

**答案**：`peek_mode` 性价比最差（+18 LUT，相对 packet_mode 翻倍）。取舍原则：只在确实需要「同一包反复读」（如重传、缓存回放）时才开；普通缓冲只开 `enable_last` 甚至纯 `minimal` 即可。

---

## 5. 综合实践

**任务**：为一个真实场景配置 FIFO 并预测其资源。

**场景**：你有一路 32 位的图像数据流，需要跨一个不长的处理停顿做缓冲，并且上游按「行」发包（每行末尾带 `last`）。要求：

1. 读侧必须等到一整行写完才能开始读，避免读到半行。
2. 偶尔上游会给出一行坏数据，需要能在写的过程中丢弃整行。
3. 不需要反复读同一行。

**请完成**：

1. 从 `enable_last`、`enable_packet_mode`、`enable_drop_packet`、`enable_peek_mode`、`enable_output_register` 中选出该开的 generic，并说明依据。
2. 选定 `depth`（假设选 1024），写出需要传给 `fifo` 实体的 generic 映射。
3. 对照 4.4.3 的面积表，估算你的配置大致占用多少 LUT/FF（提示：你的配置最接近 `fifo.with_drop_packet`）。
4. 写一个最小 testbench 验证「丢包」：用 `axi_stream_master` BFM 写一行带 `last` 的数据，中途拉一拍 `drop_packet`，再用 `axi_stream_slave` BFM 确认读侧收不到这一行（参考 [tb_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd) 的 `test_drop_packet_random_data` 用例，[L228-L253](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_fifo.vhd#L228-L253)）。

**参考要点**：
- (1) 开 `enable_last`（带行末 `last`）、`enable_packet_mode`（整行可见）、`enable_drop_packet`（丢坏行）；不开 `enable_peek_mode`（无需重读）；`enable_output_register` 视时序需要可选。
- (3) 配置与 `fifo.with_drop_packet` 一致，约 45 LUT、58 FF、1 块 Ramb36、逻辑级数 6。
- (4) `test_drop_packet_random_data` 正是断言「被 drop 的包不出现在读侧、FIFO 最终 `level=0`」的范本，可直接仿照。

## 6. 本讲小结

- `fifo` 两侧是标准的 AXI-Stream 式 ready/valid 握手：`write_ready='0'` 即满，`read_valid='0'` 即空；`level`/`almost_full`/`almost_empty` 提供状态旁路，非默认阈值会多耗逻辑。
- 读写指针用「多一位 MSB」区分满与空；`level` 用 `_next` 指针的模运算保持「永远正确」。同步 FIFO 用二进制指针即可，异步 FIFO 要换格雷码（u4-l2）。
- 四个功能 generic 层层依赖（last ← packet_mode ← drop/peek），用 `assert severity failure` 固化；不开的 `generate` 块零资源消失。
- packet_mode 用 `num_lasts_in_fifo` 计数实现「整包才可见」，drop 用指针回退，peek 用双读指针；`last` 只是 RAM 字宽 +1，几乎免费。
- `enable_output_register` 复用 u2-l1 的 `handshake_pipeline`，并把寄存器吸进 BRAM 硬输出寄存器——netlist 回归显示它几乎不增 FF。
- netlist 资源回归把「每个 generic 多少 LUT/FF」量化进 CI，是项目「面积优先」哲学的最直接体现。

## 7. 下一步学习建议

- **u4-l2 异步 FIFO**：当读写两个时钟域不同时，二进制指针不再安全，需要格雷码指针 + `resync_counter`（u3-l1）跨域，并配套 `scoped_constraints`。本讲的满/空判定与 packet_mode 逻辑会被原样复用。
- **u4-l3 hard_fifo**：直接封装 Xilinx 硬 FIFO 原语，与本文「推断式 RAM」路线互补，可对比两者在面积/时序上的取舍。
- 继续精读 [modules/fifo/src/fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd) 中 4.4.3 未展开的 `unsure_if_we_have_full_packet` 逻辑（[L380-L405](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd#L380-L405)），理解「输出寄存器 + packet_mode」组合下如何悲观地估计包完整性。
