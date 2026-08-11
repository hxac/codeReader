# 异步 FIFO 与格雷码指针（fifo_async · own_behavioural_async_fifo）

## 1. 本讲目标

本讲拆解全库最复杂的存储类 IP——异步 FIFO 的「厂商无关行为级实现」`own_behavioural_async_fifo`。学完后你应当能够：

1. 说清楚为什么跨时钟域传递 FIFO 读写指针**必须用格雷码**，而不能直接同步二进制指针。
2. 读懂 `binary_to_gray` / `gray_to_binary` 两个位运算函数的每一行。
3. 看懂「写指针同步到读时钟域」「读指针同步到写时钟域」这两个相反方向上 `ff_synchroniser_vector` 的例化。
4. 理解 Clifford Cummings 的异步 FIFO 设计方法在本库中的落地：指针多一位「折回位」、用格雷码相等判空、用特定比特模式判满。
5. 知道这层「自研」实现的叶子同步器被硬编码为 Xilinx 架构这一移植陷阱。

本讲是 u9 单元（FIFO 设计）的第三讲，承接 u8-l2 的多比特同步器、u6-l3 的双时钟双口 RAM、u9-l1 的同步 FIFO。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面几个已经在前面讲义中建立的概念：

- **同步 FIFO 与填充水位**（u9-l1）：单时钟 FIFO 用一个计数器 `fifo_fill_level` 直接得出满/空，深度不必是 2 的幂。本讲的异步 FIFO **不能**再用这种办法，原因是计数器本身也跨时钟域，同步它会撕裂。
- **双时钟双口 RAM**（u6-l3）：一块读写口分别由 `write_clk` 与 `read_clk` 驱动、无复位无使能的「哑存储」`dual_clock_dual_port_ram`。它正是异步 FIFO 的存储底座。
- **多比特同步器与数据撕裂**（u8-l2）：多比特向量若逐比特各挂同步器，会因各比特到达步调不一致而被目的域「混搭采样」，得到源端从未产生过的值——这叫**数据撕裂（data tearing）**；只有「相邻值之间每次仅翻转一比特」（汉明距离为 1）时才不会撕裂。
- **同一 entity 多架构**（u2-l1）：一个 entity 配多套 architecture，使用方用 `entity work.xxx(arch_name)` 选定其一。

本讲要回答的核心问题是：同步 FIFO 靠单一计数器判满空，那读写分处两个时钟域时，谁来告诉写端「满了」、告诉读端「空了」？答案是**各自维护自己的指针，再用格雷码把对方的指针安全地搬过时钟域边界**。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [ip/memories/fifo/fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd) | 异步 FIFO 主体，含三套 architecture。本讲精读其中的 `own_behavioural_async_fifo`，它把格雷码指针 + 多比特同步器 + 双时钟双口 RAM 串成一整套 Cummings 方法。 |
| [ip/ff_synchroniser/ff_synchroniser_vector.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd) | 多比特跨时钟域同步器。本讲只关心它如何被 FIFO 复用来同步「整条指针」。 |
| [ip/memories/ram/dual_clock_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd) | 双时钟双口 RAM，FIFO 的存储底座。 |
| [ip/memories/fifo/tb/tb_fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd) | 异步 FIFO 的 VUnit 测试台，用多个不同频率的读时钟验证「数据不丢不重」。 |
| [ip/memories/fifo/docs/async_fifo.drawio.svg](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/docs/async_fifo.drawio.svg) | draw.io 导出的结构图，可视化整条 Cummings 数据通路。建议在阅读本讲时打开对照。 |
| [ip/memories/fifo/tb/tb_fifo_async.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.do) | ModelSim/QuestaSim 波形脚本，按「指针二进制/格雷码/同步后」分组探查内部信号，是本讲跟踪指针的关键工具。 |

> 提示：本仓库的目录结构、测试约定、`.do` 脚本含义见 u1-l2 与 u1-l3。

## 4. 核心概念与源码讲解

本讲按「为什么 → 怎么算 → 怎么搬 → 怎么用」四步展开，对应四个最小模块：

- **4.1** 为什么跨域指针必须用格雷码（Cummings 方法的动机）
- **4.2** 二进制 ↔ 格雷码转换的位运算实现
- **4.3** 指针的跨时钟域同步：`ff_synchroniser_vector` 的两个方向
- **4.4** `own_behavioural_async_fifo` 的整体结构与满空判定（Cummings 方法的落地）

### 4.1 为什么跨时钟域指针必须用格雷码

#### 4.1.1 概念说明

异步 FIFO 的写端只认识 `write_clk`，读端只认识 `read_clk`，两者没有任何相位关系。要判「满」，写端必须知道读端读到了哪里（读指针）；要判「空」，读端必须知道写端写到了哪里（写指针）。也就是说，**每个指针都要从自己的时钟域，跨到对方的时钟域**。

直接同步二进制指针会出大问题。二进制计数器从 `011` 跳到 `100` 时，**三个比特同时翻转**。如果读时钟正好在这个跳变瞬间采样，三个比特各自经过同步链后到达目的域的时间略有先后，目的域可能采到 `000`、`001`、`010`、`011`、`100`、`101`、`110`、`111` 中任意一个混搭值——其中很多是源端**从未产生过**的指针值。这就是 u8-l2 讲过的数据撕裂，但后果更严重：撕裂的指针会让满/空判定出错，导致 FIFO **丢数据或重复读**。

解决办法是让指针「每次只翻一比特」。格雷码（Gray code）是一种相邻两个值之间汉明距离恒为 1 的编码。把二进制指针转成格雷码再跨域，目的域即使在跳变瞬间采样，由于只有一比特在变，采到的不是旧值就是新值，永远不会是混搭值。最坏情况只是目的域「慢一拍」看到旧指针——而 FIFO 的满空判定恰好能容忍这种滞后：读端看到的写指针偏旧 → 空标志偏保守（宁可误报空、不会读未写的数据）；写端看到的读指针偏旧 → 满标志偏保守（宁可误报满、不会覆盖未读的数据）。这正是 Cummings 方法安全性的根基。

这一整套方法来自 Clifford E. Cummings 的经典论文《Simulation and Synthesis Techniques for Asynchronous FIFO Design》，本库在文件头注释里明确标注了出处：

```vhdl
--! @details: Based on Simulation and Synthesis Techniques for
--!           Asynchronous FIFO Design by Clifford E. Cummings, Sunburst Design, Inc.
```

参见 [fifo_async.vhd:L1-L7](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L1-L7)，README 中的功能描述也对应「FIFO with separate read/write clocks and gray code pointers」。

#### 4.1.2 核心流程

把异步 FIFO 的判满空问题分解为两条对称的跨域链路：

```
写时钟域                                 读时钟域
─────────                              ─────────
write_pointer  ──binary_to_gray──┐
                                  ├──> [同步到 read_clk] ──> 读端判 empty
                                  │     （读端看写指针）
read_pointer   ──binary_to_gray──┐
                                  ├──> [同步到 write_clk] ──> 写端判 full
                                        （写端看读指针）
```

两条链路都遵循同一范式：**本地维护二进制指针 → 转成格雷码 → 用多比特同步器整条搬过边界 → 在对方域里直接用格雷码比较判满空**。

判满空之所以可以直接比格雷码（不必再转回二进制），是因为：相等的格雷码 ⟺ 相等的二进制码（用于判空）；满的比特模式在格雷码下也有等价的、固定的比较式（用于判满，见 4.4）。

#### 4.1.3 源码精读

`own_behavioural_async_fifo` 声明了「两套二进制指针 + 两套格雷码指针 + 两套跨域同步后的格雷码指针」共六组信号，这正是上面流程图的直接体现：

[fifo_async.vhd:L188-L201](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L188-L201) —— 定义指针类型与信号：

```vhdl
subtype pointer_t is unsigned(ADDRESS_WIDTH downto 0);   -- 多一位「折回位」

signal write_pointer_binary : pointer_t;
signal read_pointer_binary  : pointer_t;

signal write_pointer_gray       : std_ulogic_vector(pointer_t'range);
signal read_pointer_gray        : std_ulogic_vector(pointer_t'range);

signal write_pointer_gray_sync  : std_ulogic_vector(pointer_t'range);
signal read_pointer_gray_sync   : std_ulogic_vector(pointer_t'range);
```

注意三个层次：`*_binary` 是本地二进制指针；`*_gray` 是它转成的格雷码（跨域前的源端形态）；`*_gray_sync` 是经同步器到达对方域后的形态。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「指针跨域」这件事确实只发生在格雷码指针上，二进制指针从不跨域。

**步骤**：

1. 打开 [fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd)。
2. 找到两个同步器例化点 `write_pointer_sync` 与 `read_pointer_sync`（L258、L271）。
3. 确认它们的 `in_data` 接的是 `*_gray`（不是 `*_binary`）。
4. 确认 `*_binary` 信号**只**在本域进程里被读写，从不进入另一时钟域。

**预期结果**：你会看到二进制指针严格局限在各自的时钟进程内，跨域的「货物」永远是格雷码。这是 Cummings 方法的铁律。

#### 4.1.5 小练习与答案

**练习 1**：如果直接同步二进制指针，从 `0111` 跳到 `1000` 时，读端最坏可能采到多少种「源端从未产生过」的值？

**参考答案**：`0111→1000` 有 4 个比特同时翻转，每个比特可能采到新旧任一值，共 \(2^4 = 16\) 种组合，其中只有 `0111` 与 `1000` 两种是源端真实产生过的，其余 14 种都是撕裂值。

**练习 2**：为什么读端看到「偏旧的写指针」是安全的，而不会出错？

**参考答案**：读端用写指针判空。写指针偏旧意味着读端以为「写得更少」，于是更倾向于报 empty、暂停读取——这只会让读端**少读、保守等待**，绝不会去读尚未写入的地址，因此数据正确性不受损害，只是吞吐在边界上有轻微损失。

---

### 4.2 二进制 ↔ 格雷码转换的位运算实现

#### 4.2.1 概念说明

格雷码与二进制码之间有标准的位运算互换公式。设二进制码为 \(b_{n-1}b_{n-2}\dots b_1b_0\)，格雷码为 \(g_{n-1}g_{n-2}\dots g_1g_0\)：

- **二进制 → 格雷码**（最高位不变，其余每位是本位与高一位的异或）：

  \[
  g_{n-1} = b_{n-1}, \qquad g_i = b_i \oplus b_{i+1} \;\;(i < n-1)
  \]

- **格雷码 → 二进制码**（最高位不变，其余每位是「高一位二进制」与「本位格雷」的异或，从高到低累积）：

  \[
  b_{n-1} = g_{n-1}, \qquad b_i = b_{i+1} \oplus g_i \;\;(i < n-1)
  \]

二进制→格雷码还有一种等价的「整体右移一位再异或」写法：\(g = b \oplus (b \gg 1)\)，MSB 补 0。这正是本库 `binary_to_gray` 采用的写法。

#### 4.2.2 核心流程

`binary_to_gray` 用移位+异或一次完成；`gray_to_binary` 用一个从高到低的循环做累积异或。两者互逆。下面这张示例表把一个 4 位指针（3 位地址 + 1 位折回位，对应 8 深度的 FIFO）从 0 写到 7 再折回，便于你在 4.4.4 实践中手动核对：

| 写次数 | 二进制指针 b3b2b1b0 | 格雷码指针 g3g2g1g0 | 翻转的比特 |
| --- | --- | --- | --- |
| 0 | 0000 | 0000 | — |
| 1 | 0001 | 0001 | g0 |
| 2 | 0010 | 0011 | g1 |
| 3 | 0011 | 0010 | g0 |
| 4 | 0100 | 0110 | g2 |
| 5 | 0101 | 0111 | g0 |
| 6 | 0110 | 0101 | g1 |
| 7 | 0111 | 0100 | g0 |
| 折回(8) | 1000 | 1100 | g3（折回位） |

> 说明：上表是「示例追踪」，用一个 8 深度、3 位地址的小 FIFO 来演示格雷码每次只翻一比特的性质，不依赖测试台的具体配置。注意第 8 次写入触发折回位 g3 翻转——这正是「满」判定的依据（详见 4.4）。

#### 4.2.3 源码精读

[fifo_async.vhd:L203-L215](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L203-L215) —— 两个转换函数：

```vhdl
function binary_to_gray(binary: unsigned) return std_ulogic_vector is begin
    return std_ulogic_vector(binary xor ('0' & binary(binary'high downto 1)));
end function;

function gray_to_binary(gray: std_ulogic_vector) return unsigned is
    variable binary: unsigned(gray'range);
begin
    binary(gray'high) := gray(gray'high);
    for i in gray'high - 1 downto 0 loop
        binary(i) := binary(i + 1) xor gray(i);
    end loop;
    return binary;
end function;
```

逐行解释：

- `binary_to_gray`：`'0' & binary(binary'high downto 1)` 把二进制码整体右移一位、最高位补 0（即 \(b \gg 1\)）；再与原值异或，得 \(g = b \oplus (b \gg 1)\)。最高位与 0 异或，保持不变，正好符合 \(g_{n-1}=b_{n-1}\)。
- `gray_to_binary`：先把最高位直接拷贝（\(b_{n-1}=g_{n-1}\)）；再从次高位往下循环，每一位都是「上一位已求出的二进制」异或「本位格雷」，即 \(b_i = b_{i+1}\oplus g_i\)。注意这里用 `variable`（`:=` 立即生效），所以循环里读到的 `binary(i+1)` 是同一次循环刚算出的值，累积异或才正确。

写/读指针推进时，新格雷码直接由「旧二进制 + 1」算出，保证二进制与格雷码指针始终同步。以写指针为例：

[fifo_async.vhd:L228-L239](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L228-L239)：

```vhdl
write_pointer_logic: process (aclr, write_clk)
begin
    if aclr then
        write_pointer_binary <= (others => '0');
        write_pointer_gray   <= (others => '0');
    elsif rising_edge(write_clk) then
        if write_enable and not full then
            write_pointer_binary <= write_pointer_binary + 1;
            write_pointer_gray   <= binary_to_gray(write_pointer_binary + 1);
        end if;
    end if;
end process;
```

注意 `write_pointer_gray <= binary_to_gray(write_pointer_binary + 1)` 用的是表达式 `write_pointer_binary + 1`（基于本拍旧值），与 `write_pointer_binary` 的新值一致，二者在同一拍同步更新。

#### 4.2.4 代码实践（手算 + 仿真核对型）

**目标**：验证 `binary_to_gray` / `gray_to_binary` 互逆，且相邻格雷码值只差一比特。

**步骤**：

1. 手算：对 4 位二进制 `0101`、`1010` 分别套用公式 \(g=b\oplus(b\gg1)\)，写出格雷码。
2. 反算：对你得到的格雷码套用 `gray_to_binary` 的累积异或，确认能还原出原二进制。
3. 仿真核对（可选）：用第 5 节综合实践里 `tb_fifo_async.do` 暴露的 `write_pointer_binary` 与 `write_pointer_gray` 两个信号，在波形上逐拍比对，看格雷码是否等于「二进制 xor 右移一位」。

**预期结果**：

- `0101` → `0111`，`1010` → `1111`。
- 反算后分别还原为 `0101`、`1010`，验证互逆成立。
- 波形中任意相邻两拍 `write_pointer_gray` 只有一个比特不同。

> 若本地不具备仿真环境，步骤 1、2 的纯手算即可完成，步骤 3 标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`binary_to_gray` 里为什么是 `'0' & binary(binary'high downto 1)` 而不是 `binary(binary'high-1 downto 0)`？二者结果有区别吗？

**参考答案**：二者数值相同（都是右移一位、高位补 0），但写法上 `'0' & ... downto 1` 显式表达了「右移并补 0」的意图，且结果向量宽度与输入一致，可以直接与 `binary` 异或而不产生长度失配。可读性更好、意图更明确。

**练习 2**：`gray_to_binary` 为什么必须用 `variable` 而不能用 `signal`？

**参考答案**：累积异或要求循环里第 \(i\) 步用到第 \(i+1\) 步**刚算出来**的结果。`variable` 用 `:=` 立即更新，能保证同一次函数调用内层层传递；若用 `signal`（`<=` 延迟到下次信号更新），循环内拿不到本步结果，计算会错。

---

### 4.3 指针的跨时钟域同步：ff_synchroniser_vector 的两个方向

#### 4.3.1 概念说明

格雷码解决了「撕裂」问题，但指针仍需经过一条「多比特同步链」才能真正到达对方时钟域。这正是 u8-l2 讲过的 `ff_synchroniser_vector` 的用途：它把 `in_data_valid` 与 `in_data` 拼成一条向量，整条过同一组同步寄存器，保证各比特延迟一致、同拍到达。

异步 FIFO 需要建立**两个方向相反**的同步链路：

1. **写指针 → 读域**：让读端知道写到了哪里，用于判空与计算存量。
2. **读指针 → 写域**：让写端知道读到了哪里，用于判满。

两个方向都例化同一个 `ff_synchroniser_vector`，只是 `source_clk` / `destination_clk` 对调。链长由 generic `CDC_SYNC_STAGES` 控制（实体默认 2，与同步器内部的 `DEST_SYNC_FF` 对应），链越长 MTBF 越高（亚稳态概率指数级下降）。

#### 4.3.2 核心流程

```
方向 1（判空用）：                方向 2（判满用）：
source_clk  = write_clk          source_clk      = read_clk
destination = read_clk           destination_clk = write_clk
in_data     = write_pointer_gray in_data         = read_pointer_gray
──> write_pointer_gray_sync      ──> read_pointer_gray_sync
（在 read_clk 域被读端使用）        （在 write_clk 域被写端使用）
```

两个方向对称，各自把自己域的格雷码指针搬到对方域。

#### 4.3.3 源码精读

[fifo_async.vhd:L258-L282](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258-L282) —— 两个方向的同步器例化：

```vhdl
write_pointer_sync: entity work.ff_synchroniser_vector(xilinx_behavioural_ff_synchroniser_vector)
    generic map ( DEST_SYNC_FF => CDC_SYNC_STAGES )
    port map (
        source_clk      => write_clk,
        destination_clk => read_clk,
        in_data         => write_pointer_gray,
        in_data_valid   => '1',                 -- 格雷码恒有效，无需 valid 位
        out_data        => write_pointer_gray_sync,
        out_data_valid  => open
    );

read_pointer_sync: entity work.ff_synchroniser_vector(xilinx_behavioural_ff_synchroniser_vector)
    generic map ( DEST_SYNC_FF => CDC_SYNC_STAGES )
    port map (
        source_clk      => read_clk,
        destination_clk => write_clk,
        in_data         => read_pointer_gray,
        in_data_valid   => '1',
        out_data        => read_pointer_gray_sync,
        out_data_valid  => open
    );
```

三点要特别留意：

- **`in_data_valid => '1'` 恒接高**：u8-l2 已经讲过，格雷码保证相邻值只翻一比特，指针本身天然「每次都是有效的稳定值」，不需要额外的 valid 比特去防撕裂。于是 `out_data_valid` 也悬空（`open`）不用。
- **`DEST_SYNC_FF => CDC_SYNC_STAGES`**：把 FIFO 的同步级数 generic 透传给同步器。`CDC_SYNC_STAGES` 在 [fifo_async.vhd:L19](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L19) 定义（默认 2）。
- **移植陷阱（重要）**：两次例化都**硬编码**选了 `xilinx_behavioural_ff_synchroniser_vector`（括号里的架构名）。也就是说，名义上「厂商无关」的 `own_behavioural_async_fifo`，其叶子同步器实际上是 Xilinx `xpm_cdc_array_single` 黑盒，**仍依赖 `xpm` 库**。若要真正移植到 Intel 或纯行为级仿真，必须手动把这两处架构名改成 `intel_behavioural_ff_synchroniser_vector` 或一个自研行为级同步器。这是「自研实现分层」的一个现实例子（见 u2-l1、u8-l2 的同名提醒）。

`ff_synchroniser_vector` 内部如何把「valid + 数据」整条同步，可见 [ff_synchroniser_vector.vhd:L48-L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd#L48-L53)：

```vhdl
port map (
    src_clk   => source_clk,
    dest_clk  => destination_clk,
    src_in    => in_data_valid & in_data,   -- 拼成 (N+1) 位整条过链
    dest_out  => sync_chain_out
);
```

#### 4.3.4 代码实践（源码阅读型）

**目标**：亲手确认两个同步方向「源/目的时钟」对调，且 valid 恒为 1。

**步骤**：

1. 在 [fifo_async.vhd:L258-L282](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258-L282) 画出两张小表，分别列出 `write_pointer_sync` 与 `read_pointer_sync` 的 `source_clk`、`destination_clk`、`in_data`、`out_data`。
2. 打开 [ff_synchroniser_vector.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser_vector.vhd)，确认 `DEST_SYNC_FF` 的取值范围（L15）是 `2 to 10`，与本库 `CDC_SYNC_STAGES` 的 `positive` 类型兼容。

**预期结果**：两张表的 `source_clk`/`destination_clk` 恰好互换；两个例化的 `in_data_valid` 都是 `'1'`，`out_data_valid` 都是 `open`。

#### 4.3.5 小练习与答案

**练习 1**：把 `CDC_SYNC_STAGES` 从 2 调到 4，对 FIFO 的功能正确性和 MTBF 各有什么影响？

**参考答案**：功能正确性不变（同步链变长不改变「搬指针」的语义，只是多延迟几拍，满空判定更保守）；MTBF 显著提升，因为亚稳态经多级寄存器后被「resolve」的概率随级数指数级下降。

**练习 2**：如果把 `own_behavioural_async_fifo` 真正改成厂商无关，需要动哪些地方？

**参考答案**：至少把 [L258](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258) 与 [L271](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L271) 两处括号内的 `xilinx_behavioural_ff_synchroniser_vector` 换成 Intel 架构或一个不依赖厂商库的自研行为级多比特同步器，并相应调整该文件顶部是否还需要 `library xpm`。

---

### 4.4 own_behavioural_async_fifo 的整体结构与满空判定（Cummings 方法的落地）

#### 4.4.1 概念说明

前面三节分别解决了「为什么用格雷码」「怎么换算」「怎么跨域」。本节把它们与存储底座 `dual_clock_dual_port_ram` 串成一整套 Cummings 异步 FIFO，并讲清满/空判定。

Cummings 方法有三个关键设计决策：

1. **指针比地址多一位「折回位」（turn bit）**。深度为 \(2^N\) 的 FIFO 用 \(N\) 位地址寻址 RAM，但指针用 \(N+1\) 位。这个最高位（MSB）记录指针「绕了几圈」。空与「刚好绕一圈回到原点」的二进制指针数值相同，靠 MSB 区分二者。
2. **空用格雷码相等判定**。读端的读指针与同步过来的写指针，格雷码完全相等 → 空。
3. **满用特定比特模式判定**。写端的写指针与同步过来的读指针，满足「MSB 不同、次高位也不同、其余低位全同」→ 满。这个模式对应二进制下「写指针比读指针正好领先一圈」。

此外，存储底座用 u6-l3 的 `dual_clock_dual_port_ram`：写口接 `write_clk`、读口接 `read_clk`，只管存取，满空策略全在上层。这正符合 u6-l3 给它的定位——「哑存储」。

#### 4.4.2 核心流程

把整条通路串起来：

```
写时钟域 (write_clk)                         读时钟域 (read_clk)
────────────────────                         ────────────────────
write_pointer_binary ──binary_to_gray──┐
                                        ├── ff_synchroniser_vector ──> write_pointer_gray_sync
read_pointer_gray <──(从写域看回的读指针)─┘   (source=write_clk, dest=read_clk)
        │                                                                 │
        ├─> full 判定(写域):                                                ├─> empty 判定(读域):
        │   写指针 vs 同步后读指针                                          │   读指针 == write_pointer_gray_sync ?
        │   MSB不同 ∧ 次高不同 ∧ 低位同 => full                             │
        │                                                                 ├─> words_stored(读域):
        │                                                                 │   gray_to_binary(sync写指针) - 读指针
        ▼                                                                 ▼
   write_address = 写指针低 N 位 ──┐                          read_address = 读指针低 N 位 ──┐
                                   │                                                         │
                                   ▼                                                         ▼
                          dual_clock_dual_port_ram (write_clk 写 / read_clk 读)
```

写/读地址各取指针的低 \(N\) 位（丢弃折回位）去寻址 RAM；满空判定则用整条 \(N+1\) 位指针。

#### 4.4.3 源码精读

**(a) 指针宽度与折回位**

[fifo_async.vhd:L40-L41](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L40-L41)：

```vhdl
constant FIFO_DEPTH    : natural := 2**FIFO_DEPTH_IN_BITS;
constant ADDRESS_WIDTH : natural := to_bits(FIFO_DEPTH);
```

`ADDRESS_WIDTH` 是寻址 RAM 所需的地址位宽（由 `utils_pkg` 的 `to_bits` 给出，`to_bits` 位于 `ip/vhdl_utils` 子模块，本仓库未检入源码）。指针则比它多一位：

[fifo_async.vhd:L189](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L189)：`subtype pointer_t is unsigned(ADDRESS_WIDTH downto 0);` —— 即 `ADDRESS_WIDTH+1` 位，最高位就是折回位。地址信号则只有 `ADDRESS_WIDTH` 位：

[fifo_async.vhd:L200-L201](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L200-L201)：`write_address`/`read_address` 为 `std_ulogic_vector(ADDRESS_WIDTH - 1 downto 0)`。

**(b) 地址取指针低位**

[fifo_async.vhd:L297-L298](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L297-L298)：

```vhdl
write_address <= std_ulogic_vector(write_pointer_binary(write_address'range));
read_address  <= std_ulogic_vector(read_pointer_binary(read_address'range));
```

用 `write_address'range` 截取指针的低 `ADDRESS_WIDTH` 位作为 RAM 地址，折回位自动丢弃。

**(c) 空判定（读域，组合逻辑）**

[fifo_async.vhd:L284](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L284)：

```vhdl
empty <= '1' when read_pointer_gray = write_pointer_gray_sync else '0';
```

读端的读指针（本域）与同步过来的写指针（跨域而来）的格雷码完全相等 → 空。两个指针同时刻相等，意味着读端追上了写端。

**(d) 满判定（写域，组合逻辑）**

[fifo_async.vhd:L286-L295](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L286-L295)：

```vhdl
full_flag_detect: process (all)
    variable pointers_msbs_are_different    : boolean;
    variable addresses_msbs_are_different   : boolean;
    variable lower_address_parts_are_equal  : boolean;
begin
    pointers_msbs_are_different   := write_pointer_gray(write_pointer_gray'high)     /= read_pointer_gray_sync(read_pointer_gray_sync'high);
    addresses_msbs_are_different  := write_pointer_gray(write_pointer_gray'high - 1) /= read_pointer_gray_sync(read_pointer_gray_sync'high - 1);
    lower_address_parts_are_equal := write_pointer_gray(write_pointer_gray'high - 2 downto 0) = read_pointer_gray_sync(read_pointer_gray_sync'high - 2 downto 0);
    full <= '1' when pointers_msbs_are_different and addresses_msbs_are_different and lower_address_parts_are_equal else '0';
end process;
```

这正是 Cummings 的格雷码满判定：写指针与同步过来的读指针满足——

- 最高位（折回位）不同：说明已经绕了一圈；
- 次高位也不同：格雷码下「绕一圈」的标志性特征（回看 4.2.2 表格中第 8 次写入 g3 翻转，与之配套的还有 g2 的差异）；
- 其余低位全同：地址指回同一格。

三者同时成立 → 满。

**(e) words_stored（读域，寄存）**

[fifo_async.vhd:L217-L226](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L217-L226)：

```vhdl
words_stored_calc: process (read_clk)
    variable write_ptr_sync: unsigned(ADDRESS_WIDTH downto 0);
    variable diff: integer;
begin
    if rising_edge(read_clk) then
        write_ptr_sync := gray_to_binary(write_pointer_gray_sync);
        diff := to_integer(write_ptr_sync) - to_integer(read_pointer_binary);
        words_stored <= words_stored'subtype'high when full else diff;
    end if;
end process;
```

读端把同步过来的写指针先 `gray_to_binary` 转回二进制，再减去本域读指针，得到「读域视角」的存量。注意它是一个**滞后、保守**的近似值（因为同步链带来的延迟），满时则钳位到子类型上界。这部分细节（含读指针重放 `reset_read_pointer`）在 u9-l4 详讲。

**(f) 存储底座**

[fifo_async.vhd:L309-L318](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L309-L318) 例化 `dual_clock_dual_port_ram`，写口接 `write_clk`、读口接 `read_clk`：

```vhdl
dual_port_ram_inst: entity work.dual_clock_dual_port_ram
    port map (
        write_clk     => write_clk,
        write_enable  => write_enable and not full,   -- 满写被屏蔽
        write_data    => write_data,
        write_address => write_address,
        read_clk      => read_clk,
        read_data     => read_data,
        read_address  => read_address
    );
```

底座本身的写进程/读进程分别在两个时钟上触发，无复位无使能，干净映射为双口 BRAM（见 [dual_clock_dual_port_ram.vhd:L31-L45](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd#L31-L45)）。注意 `read_address` 不做 `read_enable and not empty` 的屏蔽——这与同步 FIFO 不同，读使能的屏蔽放在测试台/使用方一侧，RAM 每拍无条件输出当前读地址的内容。

> 旁注：读指针推进逻辑 [L241-L256](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L241-L256) 还支持 `reset_read_pointer`（把读指针清零、写指针不动），用于 SPI 多片选场景的「逐片重放」，详见 u9-l4 与 u10-l4，本讲不展开。

#### 4.4.4 代码实践（可运行型 —— 本讲主实践）

**目标**：用两个不同频率的时钟分别写读，验证异步 FIFO「数据不丢不重」，并在波形里确认写指针的格雷码每次只翻一比特。

**步骤**：

1. 按照环境（参考 u1-l3）创建 venv、安装 `vunit_hdl`，并 `git submodule update --init` 拉取 `ip/vhdl_utils`。
2. 运行 `test_runner.py`，选中 `tb_fifo_async` 这个测试台。它例化的是 `own_behavioural_async_fifo`（见 [tb_fifo_async.vhd:L791-L795](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L791-L795)）。
3. 重点看两个用例：
   - `test_different_clock_domain_combinations`（[L371-L448](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L371-L448)）：分别用「快写慢读（写 50MHz / 读 25MHz）」与「慢写快读」两种组合，写 8 个字再读回，用 `check_equal` 逐字比对。
   - `test_different_clock_speeds`（[L324-L369](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.vhd#L324-L369)）：在读取过程中用 OSVVM 随机数在 normal/slow/fast（50/25/100MHz）三种读时钟间切换。
4. 在 GUI 模式下加载 [tb_fifo_async.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.do) 波形脚本，它已把 `write_pointer_binary`、`write_pointer_gray`、`write_pointer_gray_sync` 等内部信号分组显示（[L27-L32](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_async.do#L27-L32)）。
5. 手动追踪：在波形上找出连续 8 次写使能，记录每次 `write_pointer_gray` 的值，确认任意相邻两次只有一个比特不同（对照 4.2.2 的示例表）。

**需要观察的现象**：

- 所有 `check_equal` 全部通过：写入的 8 个字（值如 300+i、800+i 等）按写入顺序读回，无丢失、无重复、无乱序。
- 即使读时钟频率随机切换，`words_stored` 与 `empty`/`full` 标志始终自洽，不会出现「读到未写入数据」或「写入覆盖未读数据」。

**预期结果**：测试台输出 `all tests passed`；波形中格雷码指针相邻值汉明距离恒为 1，同步后的指针比源端滞后若干拍但绝不撕裂。

> 待本地验证：步骤 2、3、4 依赖本地 VUnit + 仿真器环境；若环境不具备，可退化为「源码阅读型」——逐行核对 `test_different_clock_domain_combinations` 的 push/pop 队列比对逻辑，理解它如何断言「不丢不重」。

#### 4.4.5 小练习与答案

**练习 1**：为什么「满」要用「MSB 不同 ∧ 次高位不同 ∧ 低位同」三个条件，而「空」只需「整条格雷码相等」？

**参考答案**：空发生在两指针完全相同（读追上写），所以整条相等即可。满发生在写比读正好领先一圈（地址指回同一格，但折回位不同）；在格雷码下，「领先一圈」的标志是最高两位都不同而低位相同——这正是 Cummings 推导出的、可直接在格雷码域判定的满模式，无需转回二进制。

**练习 2**：满/空判定都用的是「同步过来的对方指针」，这个滞后会不会让 FIFO 出现「假满」或「假空」？是否影响正确性？

**参考答案**：会出现保守的「假满」「假空」（写端以为读得少 → 误报满；读端以为写得少 → 误报空），但**不影响正确性**：假满只是让写端少写一拍、假空只是让读端多等一拍，都不会导致覆盖未读数据或读取未写数据。代价是峰值吞吐略降，换来的是跨时钟域的绝对安全。

---

## 5. 综合实践：跟踪一次完整跨域写读

把本讲四个模块串成一个端到端的小任务，建议在带 GUI 的仿真器中完成：

1. **配置**：运行 `tb_fifo_async`，把读时钟切到 `slow`（25MHz）、写时钟保持 50MHz（即「快写慢读」），这正是 `test_different_clock_domain_combinations` 的 5.1 子场景。
2. **观察写域**：触发 8 次写入。用 `tb_fifo_async.do` 的分组，同时看 `write_pointer_binary` 与 `write_pointer_gray`：
   - 确认二进制指针每次 +1；
   - 确认格雷码指针每次只翻一比特（对照 4.2.2 表）；
   - 在写域看 `read_pointer_gray_sync`，确认它滞后于读端本地的 `read_pointer_gray` 几拍。
3. **观察读域**：慢速读回这 8 个字。看 `write_pointer_gray_sync` 何时到达读域、`empty` 何时解除断言、`words_stored` 如何随之上升。
4. **核对数据**：每个 `fifo_read_data_valid` 拉高的拍，确认 `fifo_read_data` 等于当初写入的值，且顺序一致。
5. **断点思考**：在波形里找到一次「写端因 `full` 暂停」或「读端因 `empty` 等待」的瞬间，解释它为何是「保守暂停」而非错误。

**产出**：一张包含 `write_clk`、`read_clk`、`write_pointer_gray`、`write_pointer_gray_sync`、`empty`、`full`、`read_data_valid`、`read_data` 的波形截图，并在其上标注「格雷码单比特翻转」「同步延迟」「假满/假空」三类时刻。

> 待本地验证：本实践需要本地仿真器与厂商/行为级同步链可编译；若仅做阅读，请逐行跟踪 `test_different_clock_domain_combinations` 中 push→pop 的队列顺序，论证「不丢不重」。

## 6. 本讲小结

- 异步 FIFO 的满空判定需要把对方的指针搬过时钟域；**直接同步二进制指针会撕裂**，必须用每次只翻一比特的**格雷码**。
- 本库用标准位运算实现互换：`binary_to_gray` 是 \(g=b\oplus(b\gg1)\)，`gray_to_binary` 是从高到低的累积异或（必须用 `variable`）。
- 两条对称的 `ff_synchroniser_vector` 链路分别把写指针搬到读域（判空）、读指针搬到写域（判满）；`in_data_valid` 恒接 `'1'`，因为格雷码本身免撕裂。
- Cummings 方法的落地：指针比地址多一位「折回位」；**空 = 两格雷码指针相等**；**满 = 最高两位不同且低位相同**；满空都因同步滞后而「保守」，绝不影响正确性。
- 存储底座是 u6-l3 的 `dual_clock_dual_port_ram`，地址取指针低位、折回位丢弃；满写被 `write_enable and not full` 屏蔽。
- **移植陷阱**：`own_behavioural_async_fifo` 的叶子同步器被硬编码为 `xilinx_behavioural_ff_synchroniser_vector`，仍依赖 `xpm` 库；真正厂商无关需手动改两处例化。

## 7. 下一步学习建议

- 进入 **u9-l4（异步 FIFO 的满空标志与读指针重放）**：细读 `words_stored` 的钳位逻辑、`reset_read_pointer` 数据重放机制，以及测试台如何用断言覆盖这些边界。
- 回顾 **u10-l4（SPI 顶层接口）**：那里会把本讲的 `fifo_async` 当作 TX FIFO，配合多片选状态机用 `reset_read_pointer` 实现「同一批数据逐片重放」，是异步 FIFO 的真实用例。
- 若想验证「撕裂」确实致命，可尝试写一个实验性测试台：把 `fifo_async` 内部临时改成同步二进制指针（**仅本地实验，勿提交**），用快慢时钟压测，观察 `check_equal` 在哪里先失败——这是理解格雷码必要性的最直观方式。
