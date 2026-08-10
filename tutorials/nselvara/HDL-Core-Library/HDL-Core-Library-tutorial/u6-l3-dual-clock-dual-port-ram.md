# 双时钟双口 RAM

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「双时钟双口 RAM（dual-clock dual-port RAM）」与上一讲「双口 RAM」的根本区别：写口和读口分别由**两个相互独立的时钟** `write_clk` 与 `read_clk` 驱动，两条路径处于不同的时钟域。
- 读懂本模块把存储体 `ram_reg` 声明成 **signal（信号）**（而非上一讲的 variable）的原因，以及它如何被一个进程写、另一个进程读——理解这一点也就理解了为什么本模块不再需要担心「同周期同地址读写顺序」的问题。
- 掌握用 `std_ulogic_vector` 地址配合 `unsigned()` 类型转换再 `to_integer` 的寻址写法，并明白它和上一讲直接用 `unsigned` 地址的差异。
- 解释为什么这块「哑存储」——不带复位、不带满空标志、不带读使能——恰好是异步 FIFO 最理想的存储底座，并能在 `fifo_async.vhd` 中追踪到它的例化位置。

## 2. 前置知识

进入本讲前，请确认你熟悉以下概念（大多已在 u6-l1、u6-l2 建立）：

- **同步 RAM 的读写**：RAM 是按地址访问的存储阵列；本库的 RAM 读写都发生在时钟上升沿（`rising_edge(...)`）。
- **signal 与 variable 的差别**：signal 用 `<=` 赋值、更新推迟到 delta 周期之后；variable 用 `:=` 赋值、立即更新。u6-l2 已经讲过：`dual_port_ram` 为了精确控制「同周期同地址读写」的顺序，特意把存储体声明成 variable。
- **时钟域（clock domain）与跨时钟域（CDC）**：一块 FPGA 里可能同时存在多个频率/相位各异的时钟。由同一个时钟驱动的寄存器属于同一时钟域；信号从一个时钟域传到另一个时钟域，就叫跨时钟域。跨时钟域传递信号需要「同步器」（见第 8 单元），否则会出现亚稳态。
- **FIFO（先进先出队列）**：一种「一头写入、另一头读出、先写进的先读出」的缓冲结构。若写口和读口用同一个时钟，叫同步 FIFO；若用两个不同时钟，叫异步（双时钟）FIFO。异步 FIFO 是跨时钟域搬数据的经典手段。

> 本讲是 u6-l2「双口 RAM」的直接续篇，会频繁把两者对照。如果你还没读过 `dual_port_ram.vhd`，建议先快速浏览一遍它的端口与进程结构再回来。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| [ip/memories/ram/dual_clock_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd) | 设计源码（可综合） | 双时钟双口 RAM 主体，本讲核心。 |
| [ip/memories/fifo/fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd) | 设计源码（使用方） | 异步 FIFO，本模块最主要的使用者，例化见 [L309-L318](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L309-L318)。 |
| [ip/memories/fifo/fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) | 设计源码（使用方） | 同步 FIFO 的自研实现也复用了本模块（把两个时钟都接成 `sys_clk`），例化见 [L231-L240](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L231-L240)。 |
| [ip/memories/ram/dual_port/dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd) | 设计源码（对照参考） | 上一讲的单时钟双口 RAM，用于逐项对比。 |

一句话定位：`dual_clock_dual_port_ram.vhd` 是一块「**读写分别由两个独立时钟驱动**、不带任何控制逻辑」的纯存储原语；它解决的核心问题是——**给异步 FIFO 提供一块可以在两个异步时钟域之间安全存取数据的存储体**。

> 另请注意：本模块在仓库中**没有独立的测试台**（`tb/`），它靠 `fifo_sync` / `fifo_async` 等使用方的测试台间接覆盖。这一点在 u1-l2 的目录结构里已经提过，所以本讲的代码实践以「源码阅读 + 自写最小测试台」为主。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **dual_clock_dual_port_ram：双时钟双口结构**（模块的端口与存储类型）
2. **独立读写时钟进程**（写进程、读进程、为何用 signal、寻址写法）
3. **异步 FIFO 存储底座**（它如何被 `fifo_async` 使用、为何适合做跨时钟域存储）

---

### 4.1 dual_clock_dual_port_ram：双时钟双口结构

#### 4.1.1 概念说明

上一讲的 `dual_port_ram` 是「**同一个时钟**、独立的读写两口」：写口和读口共用一个 `sys_clk`，读写发生在同一个时钟沿上，所以才需要讨论「同周期同地址读写时读旧值还是新值」。

本讲的 `dual_clock_dual_port_ram` 把这件事推向了极致：**写口和读口各用各的时钟**。

- 写口的全部动作（采样 `write_address`、在 `write_enable` 有效时把 `write_data` 写入存储体）只看 `write_clk`；
- 读口的全部动作（采样 `read_address`、把存储体对应位置的内容送到 `read_data`）只看 `read_clk`；
- `write_clk` 和 `read_clk` 之间**没有任何假定**——可以频率不同、相位不同，甚至来自不同的晶振，彼此完全异步。

这一点从端口的命名就能一眼看出来：

| 端口 | 所属时钟域 | 作用 |
| --- | --- | --- |
| `write_clk` | 写域 | 写口时钟 |
| `write_enable`、`write_data`、`write_address` | 写域 | 写控制与写数据/地址 |
| `read_clk` | 读域 | 读口时钟 |
| `read_address`、`read_data` | 读域 | 读地址与读数据 |

注意几个与 `dual_port_ram` 相比「**少了**」的东西，它们都被刻意拿掉了：

- **没有 `sys_rst_n` 复位端口**：整块 RAM 不在模块内部复位（原因见 4.1.3 与 4.3）。
- **没有 `en` 总使能，也没有 `read_enable`**：读口在每个 `read_clk` 上升沿都无条件地把 `read_address` 处的内容输出，是否「有效」交给上层 FIFO 用 `read_data_valid` 去管。

这种「能省则省」的极简端口，正是为了让它当一块纯粹的「哑存储」——这一点会在 4.3 解释为什么它特别适合做 FIFO 的存储体。

#### 4.1.2 核心流程

模块在每个时钟沿的行为可概括为两条互不相干的流水：

```
写域（由 write_clk 驱动）：
  write_clk 上升沿到来：
    └── write_enable = 1 ?
          └── ram_reg[write_address] <= write_data    # 写入存储体

读域（由 read_clk 驱动）：
  read_clk 上升沿到来：
    └── read_data <= ram_reg[read_address]            # 无条件读出（无 read_enable）
```

设写地址位宽为 \(N\)，则存储深度为：

\[
\text{RAM\_DEPTH} = 2^{N}
\]

例如写地址 3 位时，深度为 8，即 8 个存储单元；数据位宽则由 `write_data` 的实际位宽决定（非约束端口，例化时才确定）。

两条流水唯一的交集，是它们读写**同一个存储体** `ram_reg`——这是「双口」二字的物理含义：同一块存储，两个端口各自访问。

#### 4.1.3 源码精读

先看 entity 的端口声明：

[dual_clock_dual_port_ram.vhd:L13-L23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd#L13-L23) —— 两个时钟、分离的读写口，且**没有复位、没有 `en`、没有 `read_enable`**。

```vhdl
entity dual_clock_dual_port_ram is
    port (
        write_clk: in std_ulogic;
        write_enable: in std_ulogic;
        write_data: in std_ulogic_vector;
        write_address: in std_ulogic_vector;
        read_clk: in std_ulogic;
        read_data: out std_ulogic_vector;
        read_address: in std_ulogic_vector
    );
end entity;
```

把这份端口表和 `dual_port_ram` 的端口（[dual_port_ram.vhd:L13-L25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L13-L25)）并排放，差异一目了然：

| 维度 | `dual_port_ram`（u6-l2） | `dual_clock_dual_port_ram`（本讲） |
| --- | --- | --- |
| 时钟个数 | 1 个 `sys_clk` | 2 个：`write_clk` + `read_clk` |
| 复位 | 有 `sys_rst_n` | **无** |
| 总使能 | 有 `en` | **无** |
| 读使能 | 有 `read_enable` | **无**（每拍都读） |
| 地址类型 | `unsigned` | `std_ulogic_vector` |

再看 architecture 里的存储类型推导：

[dual_clock_dual_port_ram.vhd:L25-L29](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd#L25-L29) —— 在 architecture（而非进程）说明区推导存储类型，并用 **signal** 声明存储体。

```vhdl
architecture behavioural of dual_clock_dual_port_ram is
    subtype ram_depth_t is natural range 2**write_address'length - 1 downto 0;
    type ram_t is array (ram_depth_t) of write_data'subtype;

    signal ram_reg: ram_t;
begin
```

三个细节值得圈出：

1. **深度同样由地址位宽推导**：`2**write_address'length`，和单口/双口 RAM 思路一致；只是这里没有显式定义 `RAM_DEPTH` 常量，而是直接把表达式写进了 `ram_depth_t` 的上下界里。
2. **索引范围用的是 `downto`（降序）**：`natural range 2**write_address'length - 1 downto 0`。`dual_port_ram` 里对应写法是 `natural range 0 to RAM_DEPTH - 1`（升序 `to`）。两者包含的合法下标值集合相同（都是 \(0 \dots 2^N-1\)），数组元素个数也相同，只是范围的「书写方向」不同——这是一个不影响行为、但确实存在于源码里的风格差异。
3. **`ram_reg` 是 signal**，而且声明在 **architecture 说明区**。这一点是本讲的关键之一，下一节展开：它直接决定了「为什么本模块不再纠结读写顺序」。

#### 4.1.4 代码实践

**实践目标**：用「端口对照」建立对本模块结构的第一印象，确认它确实是一块极简的跨域存储。

**操作步骤**：

1. 打开 [dual_clock_dual_port_ram.vhd:L13-L23](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd#L13-L23) 与 [dual_port_ram.vhd:L13-L25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L13-L25)，逐行比对端口。
2. 数一数本模块的输入端口里，有几个是「写域」信号、几个是「读域」信号。
3. 注意本模块**完全没有 `rst` / `en` / `read_enable`**，思考：这些功能是被永久删掉了，还是被挪到别处（上层 FIFO）去实现了？

**需要观察的现象 / 预期结果**：写域 4 个信号（`write_clk`/`write_enable`/`write_data`/`write_address`），读域 3 个信号（`read_clk`/`read_address`/`read_data`，其中 `read_data` 是输出）。复位与使能确实不在本模块内，它们由 FIFO 用「复位/管理读写指针」的方式在上层实现（见 4.3）。

#### 4.1.5 小练习与答案

**练习 1**：本模块没有 `read_enable`，那读口会不会一直「乱读」、浪费什么？

**参考答案**：不会「浪费」——读口每拍把 `read_address` 处的内容送到 `read_data`，这是寄存器输出的自然行为，本身不消耗额外控制。「该不该采纳这次读出的数据」由上层 FIFO 用 `read_data_valid`（在 FIFO 里由 `read_enable and not empty` 产生）来判断。把「读」和「读是否有效」解耦，正是 FIFO 这类结构常见的分工。

**练习 2**：为什么本模块不在内部加一个 `aclr` 异步复位端口，像 `fifo_async` 那样？

**参考答案**：因为本模块服务于两个异步时钟域。一个异步复位若同时作用于写进程和读进程，其释放沿本身又是一个跨域事件，处理不好会引入新的亚稳态风险；而且（和单口/双口 RAM 一样）一拍清空整块存储体无法映射成片上 BRAM。所以本模块干脆不碰复位，改由上层 FIFO 复位**指针**（指针复位后，旧的存储内容自然被新写入覆盖），既简单又能保住 BRAM 推断。

---

### 4.2 独立读写时钟进程

#### 4.2.1 概念说明

本模块的核心是**两个互相独立的进程**：`write_process` 只对 `write_clk` 敏感，`read_process` 只对 `read_clk` 敏感。它们共享同一个存储体 `ram_reg`，但除此以外毫无瓜葛。

这里要回答一个关键问题：**为什么本模块的 `ram_reg` 是 signal，而上一讲 `dual_port_ram` 的 `ram_reg` 是 variable？**

回忆 u6-l2 的结论：`dual_port_ram` 之所以用 variable，是为了让「同周期同地址读写」时，进程内语句的先后顺序能精确决定读到新值还是旧值——因为读写发生在**同一个时钟进程的同一个上升沿**里，存在「读改写」问题。

而本模块里，写和读分别在**两个不同时钟域的进程**里：

- `write_process` 在 `write_clk` 上升沿写 `ram_reg`；
- `read_process` 在 `read_clk` 上升沿读 `ram_reg`。

这两个上升沿在时间上**根本不同步**，不存在「同一个时钟沿里既读又写」这回事。`read_process` 每次 `read_clk` 到来时，看到的只是 `ram_reg` 此刻的快照——至于这个值是几个 `write_clk` 之前写进来的，由上层 FIFO 用指针和同步器去保证一致性，与本存储体无关。

因此本模块**没有任何「读写顺序」需要纠结**，自然也就不需要 variable；用 signal 即可。事实上，用一个 signal 把存储体在两个进程间共享，正是 VHDL 描述「真双口 RAM」的标准写法：

- `ram_reg` 的**唯一驱动者**是 `write_process`（它对 `ram_reg(...)` 赋值）；
- `read_process` 只**读** `ram_reg(...)`，不驱动它；
- 仿真器/综合工具据此推断出一块「一口写、一口读」的双口存储。

> 与 u6-l2 的呼应：上一讲那句「不要把写块挪到读块之上」的警告，在本模块里**不存在**——因为读和写根本不在同一个进程里，谈不上「块的前后顺序」。这是「单时钟双口」与「双时钟双口」在源码层面的本质分野。

还有一个寻址细节：本模块的地址端口是 `std_ulogic_vector`，而 `ram_reg` 的下标必须是整数。VHDL 里 `std_ulogic_vector` 不能直接转整数，必须先用 `numeric_std` 包里的 `unsigned()` 把它「解释」成无符号数，再 `to_integer` 转成自然数下标。这与 `dual_port_ram`（地址直接声明为 `unsigned`，因此只写 `to_integer(read_address)`）形成对照。

#### 4.2.2 核心流程

两个进程的伪代码：

```
write_process (敏感于 write_clk):
  if rising_edge(write_clk) then
     if write_enable then
        ram_reg[ to_integer(unsigned(write_address)) ] <= write_data   # 唯一驱动 ram_reg 的地方
     end if
  end if

read_process (敏感于 read_clk):
  if rising_edge(read_clk) then
     read_data <= ram_reg[ to_integer(unsigned(read_address)) ]        # 只读，不驱动 ram_reg
  end if
```

读输出的延迟：`read_data` 是 `<=` 赋值的 signal，所以读出的数据相对 `read_address` 晚一个 `read_clk` 周期出现在端口上（和单口/双口 RAM 一致）。即：

\[
t_{\text{read\_data}} = t_{\text{read\_address}} + 1 \text{（个 read\_clk 周期）}
\]

#### 4.2.3 源码精读

写进程：

[dual_clock_dual_port_ram.vhd:L31-L38](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd#L31-L38) —— 仅由 `write_clk` 触发；`write_enable` 有效时用 `<=` 写入存储体。

```vhdl
write_process: process (write_clk)
begin
    if rising_edge(write_clk) then
        if write_enable then
            ram_reg(to_integer(unsigned(write_address))) <= write_data;
        end if;
    end if;      
end process;
```

读进程：

[dual_clock_dual_port_ram.vhd:L40-L45](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd#L40-L45) —— 仅由 `read_clk` 触发；无 `read_enable`、无复位，每拍无条件把 `read_address` 处的内容寄存到 `read_data`。

```vhdl
read_process: process (read_clk)
begin
    if rising_edge(read_clk) then
        read_data <= ram_reg(to_integer(unsigned(read_address)));
    end if;
end process;
```

逐条对照上一讲的 `dual_port_ram`：

| 维度 | `dual_port_ram`（u6-l2） | `dual_clock_dual_port_ram`（本讲） |
| --- | --- | --- |
| 进程个数 | 1 个（`mem_operation_proc`，敏感于 `sys_clk`） | 2 个（`write_process` / `read_process`） |
| 存储体类型 | `variable ram_reg`（进程内，`:=` 立即更新） | `signal ram_reg`（architecture 区，`<=` 延迟更新） |
| 寻址 | `to_integer(read_address)`（地址已是 `unsigned`） | `to_integer(unsigned(read_address))`（地址是 `std_ulogic_vector`） |
| 读写顺序问题 | 有（read-first / write-first） | 无（两进程不同时钟域，无共同沿） |

关于寻址那行 `to_integer(unsigned(write_address))` 再多说一句：`unsigned(...)` 是 `numeric_std` 提供的类型转换函数，它并不改变位模式，只是告诉编译器「请把这串 `std_ulogic` 当作无符号二进制数来解释」，之后 `to_integer` 才把它算成自然数下标。两步缺一不可。

最后注意：两个进程里都**没有复位分支**。这意味着上电后 `ram_reg` 的内容是 `'U'`（未初始化），必须「先写后读」才有意义——这和单口/双口 RAM「复位不清存储体」的取舍一脉相承，目的是让综合工具推断出片上 BRAM。

#### 4.2.4 代码实践

**实践目标**：确认 `ram_reg` 的「单写者、单读者」关系，并理解寻址转换的两步。

**操作步骤**：

1. 在 [dual_clock_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd) 里全文搜索 `ram_reg`，数一数它被赋值（`<=`）几次、被读取几次。
2. 确认：对 `ram_reg` 赋值只出现在 `write_process`（L35）这一处；读取出现在 `read_process`（L43）这一处。
3. 自己心算一次寻址：若 `write_address = "011"`（3 位），`unsigned("011")` 是多少？`to_integer(...)` 又是多少？应该写入哪个下标？

**需要观察的现象 / 预期结果**：`ram_reg` 只有一个驱动者（`write_process`），一个读者（`read_process`）——这正是「一口写、一口读」双口 RAM 的标准结构。`unsigned("011")` 即无符号数 3，`to_integer` 得 3，写入下标 3。

#### 4.2.5 小练习与答案

**练习 1**：假如有人把本模块的 `ram_reg` 改成 `variable` 并搬进某个进程，会发生什么？

**参考答案**：语法上 variable 只能存在于一个进程/子程序内，无法被另一个时钟域的进程看见——那样 `read_process` 就读不到这块存储了。换句话说，**跨进程共享存储体必须用 signal**。本模块用 signal 不是「图省事」，而是「跨时钟域双口」的内在要求。

**练习 2**：为什么本模块的地址端口声明成 `std_ulogic_vector`，而 `dual_port_ram` 声明成 `unsigned`？这对使用方有影响吗？

**参考答案**：这是设计者的风格选择。`std_ulogic_vector` 更接近「原始数据线」，使用方（如 `fifo_async`）的地址信号也常常是 `std_ulogic_vector`（由指针拼接/截取而来），这样端口对接时类型一致、少一层转换。代价是模块内部寻址要多写一步 `unsigned(...)`。对使用方而言，只要连线类型匹配即可，行为不受影响。

---

### 4.3 异步 FIFO 存储底座

#### 4.3.1 概念说明

把 4.1、4.2 的事实合起来看：`dual_clock_dual_port_ram` 是一块**只管存取、不管策略**的极简存储——

- 它不维护「满」「空」标志；
- 它不维护读写指针；
- 它不带复位、不带使能；
- 它只做两件事：在 `write_clk` 沿按 `write_address` 写、在 `read_clk` 沿按 `read_address` 读。

这恰好是**异步 FIFO** 想要的存储底座。原因在于异步 FIFO 的设计分工（这是 Cliff Cummings 在《Simulation and Synthesis Techniques for Asynchronous FIFO Design》里确立的经典方法，`fifo_async.vhd` 文件头第 4 行就引用了它）：

| 职责 | 由谁承担 |
| --- | --- |
| 实际存取数据 | **本模块** `dual_clock_dual_port_ram` |
| 维护写指针、递增写地址 | FIFO 的写域逻辑（`write_pointer_logic` 进程） |
| 维护读指针、递增读地址 | FIFO 的读域逻辑（`read_pointer_logic` 进程） |
| 指针跨时钟域同步 | `ff_synchroniser_vector`（第 8 单元，格雷码同步） |
| 满空标志判定 | FIFO 用「同步后的对方指针」与本域指针比较 |
| 复位 | FIFO 复位指针（`aclr`），不动存储体 |

也就是说，**所有「聪明」的策略都在 FIFO 层完成，存储体只需要「够笨、够通用、能跨域」**。本模块因为剥离了一切控制逻辑，反而成了最合适的那个「笨存储」：

1. **两个独立时钟端口** → 天然贴合两个异步时钟域，不需要在模块内部做任何同步。
2. **无复位、无使能** → 上层 FIFO 用指针管理一切，存储体始终保持「哑」状态，综合时干净地映射成一块双口 BRAM。
3. **读口每拍输出** → 配合 FIFO 的读指针递增，顺次读出数据，时序简单可预期。
4. **signal 存储体 + 两进程** → 不存在读写顺序问题，跨域语义清晰。

一个值得注意的连带结论：正因为读、写分处不同时钟域，**本模块不存在 u6-l2 讨论的「同周期同地址 read-during-write」问题**。在异步 FIFO 的实际运行中，满空标志机制保证了「写指针追上读指针（满）」「读指针追上写指针（空）」这些边界不会让两边真正冲突；至于「写和读恰好落到同一地址」的情况，由 Cummings 方法的指针设计（多一位折回位 + 格雷码）天然规避。所以本模块可以放心用最朴素的 signal 双口写法。

#### 4.3.2 核心流程

以异步 FIFO 的「写一个字」为例，追踪从应用到存储体的完整路径：

```
应用层：拉高 write_enable，给出 write_data
   │
   ▼
fifo_async 写域：write_pointer_logic 进程
   ├── 检查 not full
   ├── 生成 write_address（取写指针低位）
   └── 把 write_address / write_data / write_clk 送给 ↓
   │
   ▼
dual_clock_dual_port_ram：write_process
   └── 在 write_clk 沿把 write_data 写入 ram_reg[write_address]
```

读路径对称：`read_pointer_logic` 生成 `read_address`，送给本模块的 `read_process`，在 `read_clk` 沿把数据读到 `read_data`。满空标志则由两个方向各自同步对方的格雷码指针后比较得到——完全在 FIFO 层完成，与本存储体无关。

#### 4.3.3 源码精读

`fifo_async` 的自研架构 `own_behavioural_async_fifo` 在末尾例化了本模块：

[fifo_async.vhd:L309-L318](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L309-L318) —— 把写域信号接写口、读域信号接读口，两个时钟各自接到对应的指针逻辑时钟域。

```vhdl
dual_port_ram_inst: entity work.dual_clock_dual_port_ram
    port map (
        write_clk => write_clk,
        write_enable => write_enable and not full,
        write_data => write_data,
        write_address => write_address,
        read_clk => read_clk,
        read_data => read_data,
        read_address => read_address
    );
```

注意 `write_enable => write_enable and not full`：FIFO 在存储体之外**额外**叠加了「满则不写」的保护（`write_address`、`read_address` 则来自 [fifo_async.vhd:L297-L298](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L297-L298)，由二进制指针截取而来）。这正印证了 4.3.1 的分工：流量控制由 FIFO 负责，存储体只负责存取。

> **同样的模块，同步 FIFO 也用**：`fifo_sync` 的自研实现里也能看到一模一样的例化（[fifo_sync.vhd:L231-L240](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L231-L240)），只是它把 `write_clk` 和 `read_clk` **都接成了 `sys_clk`**——「双时钟双口 RAM」退化成「单时钟双口 RAM」依然能正常工作。一份存储原语同时服务同步/异步两种 FIFO，这正是把控制逻辑剥离到上层带来的复用红利。

#### 4.3.4 代码实践

**实践目标**：在 `fifo_async.vhd` 里完整追踪一次「写指针 → 写地址 → 存储体」的连线，亲眼看到本模块处在调用链的叶子位置。

**操作步骤**：

1. 打开 [fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd)，定位 `write_pointer_logic` 进程（[L228-L239](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L228-L239)），看 `write_pointer_binary` 如何递增。
2. 看 [L297](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L297) 的 `write_address <= std_ulogic_vector(write_pointer_binary(write_address'range));`——写地址就是写指针的低位。
3. 顺着 `write_address` 这根线到 [L314](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L314)，它正好接进本模块的 `write_address` 端口。
4. 同样追踪 `read_address`（[L298](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L298) → [L317](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L317)）。

**需要观察的现象 / 预期结果**：你会看到一条清晰的链路——「指针逻辑生成二进制指针 → 截取低位作地址 → 喂给本模块」。本模块既不生成地址、也不判断满空，纯粹是这条链路的末端「执行者」。

#### 4.3.5 小练习与答案

**练习 1**：如果把这块存储体换成 u6-l2 的 `dual_port_ram`（单时钟双口），异步 FIFO 还能正常工作吗？

**参考答案**：不能直接用。`dual_port_ram` 只有一个 `sys_clk`，无法把写口接 `write_clk`、读口接 `read_clk`。强行把读写都接同一个时钟，就退化成同步 FIFO 了。异步 FIFO 必须用「两个独立时钟端口」的存储体，这正是 `dual_clock_dual_port_ram` 存在的理由。

**练习 2**：本模块没有 `full` / `empty` 输出，FIFO 怎么知道存储体满了还是空了？

**参考答案**：满空根本不由存储体判断，而由 FIFO 比较「写指针」与「读指针」得到（详见 u9）。具体地，`full` 看两个指针的 MSB 折回位与低位是否满足特定关系（[fifo_async.vhd:L286-L295](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L286-L295)），`empty` 看两者格雷码是否相等（[L284](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L284)）。存储体对此一无所知——它只负责存，策略全在 FIFO 层。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个贯穿任务（对应本讲的实践要求）。

**任务**：为 `dual_clock_dual_port_ram` 写一个**最小自验证测试台**：用两个不同频率的时钟，分别「写满 8 个单元」再「读回」，验证数据一致性；最后用一段话说明它为什么适合做跨时钟域 FIFO 的存储体。

> 说明：仓库中**没有**本模块的现成测试台（它靠 FIFO 的测试台间接覆盖）。下面的测试台是**示例代码**，需要你自行创建文件、按 u1-l3 的方式跑通，并补全读回校验的时序对齐。

**要求与提示**：

1. **沿用本库的测试台风格**：参考 [tb_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd) 的骨架（`generate_advanced_clock` 用法见 [L67](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L67)，VUnit `runner_cfg` / `test_runner_setup` 等见 u11-l1），但这里要**生成两个不同频率的时钟**。
2. **写阶段**：用 `write_clk`（例如 100 MHz）向地址 0…7 依次写入 8 个已知值（如 \(i \times 0x11\)）。
3. **读阶段**：用 `read_clk`（例如 50 MHz，刻意与写时钟不同频）依次读回地址 0…7。注意 `read_data` 相对 `read_address` 晚一个 `read_clk` 周期，比对时要错开一拍。
4. **校验**：用 `check_equal` 比对读回值与预期值。

下面是一个起点骨架（**示例代码**，仓库中不存在，需自行创建完善）：

```vhdl
-- 示例代码：双时钟双口 RAM 的最小自验证测试台（需自行创建并完善）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library vunit_lib;
context vunit_lib.vunit_context;

use work.tb_utils.all;

entity tb_dual_clock_dual_port_ram is
    generic (
        runner_cfg: string := runner_cfg_default
    );
end entity;

architecture tb of tb_dual_clock_dual_port_ram is
    constant ADDR_WIDTH : positive := 3;          -- 3 位地址 → 深度 2**3 = 8
    constant DATA_WIDTH : positive := 8;

    signal write_clk  : std_ulogic := '0';
    signal read_clk   : std_ulogic := '0';
    signal clk_enable : std_ulogic := '1';

    signal write_enable  : std_ulogic := '0';
    signal write_address : std_ulogic_vector(ADDR_WIDTH-1 downto 0) := (others => '0');
    signal write_data    : std_ulogic_vector(DATA_WIDTH-1 downto 0) := (others => '0');
    signal read_address  : std_ulogic_vector(ADDR_WIDTH-1 downto 0) := (others => '0');
    signal read_data     : std_ulogic_vector(DATA_WIDTH-1 downto 0);
begin
    -- 两个不同频率的时钟：写 100 MHz，读 50 MHz
    generate_advanced_clock(write_clk, real(100e6), 0 fs, clk_enable);
    generate_advanced_clock(read_clk,  real(50e6),  0 fs, clk_enable);

    dut: entity work.dual_clock_dual_port_ram
        port map (
            write_clk     => write_clk,
            write_enable  => write_enable,
            write_data    => write_data,
            write_address => write_address,
            read_clk      => read_clk,
            read_data     => read_data,
            read_address  => read_address
        );

    stim: process
    begin
        test_runner_setup(runner, runner_cfg);

        -- 1) 写满 8 个单元：地址 0..7 分别写入 0x00, 0x11, 0x22, ...
        for i in 0 to 7 loop
            wait until rising_edge(write_clk);
            write_enable  <= '1';
            write_address <= std_ulogic_vector(to_unsigned(i, ADDR_WIDTH));
            write_data    <= std_ulogic_vector(to_unsigned(i*16#11#, DATA_WIDTH));
        end loop;
        wait until rising_edge(write_clk);
        write_enable <= '0';

        -- 2) 读回 8 个单元（read_data 晚 read_address 一个 read_clk 周期，比对需错开一拍）
        --    此处给出读地址的驱动，具体 check_equal 的对齐请你在本地补全。
        for i in 0 to 7 loop
            wait until rising_edge(read_clk);
            read_address <= std_ulogic_vector(to_unsigned(i, ADDR_WIDTH));
            -- TODO: 在下一个 read_clk 沿后用 check_equal(read_data, expected(i)) 校验
        end loop;

        test_runner_cleanup(runner);
        wait;
    end process;
end architecture;
```

**需要观察的现象 / 预期结果**：

1. 即便 `write_clk`（100 MHz）是 `read_clk`（50 MHz）的两倍快，写进去的 8 个值仍能被正确读回——因为两个时钟域各自独立地驱动各自的口，存储体本身不关心两者的频率比。
2. `read_data` 总比 `read_address` 晚一个 `read_clk` 周期出现（读寄存器输出），校验时必须错开一拍，否则会比对到一个「旧地址」的数据。

**最后的说明（为何适合做跨时钟域 FIFO 存储体）**：因为它把「存取」与「策略」彻底解耦——两个独立时钟端口天然贴合两个异步域、无复位无使能让它干净映射成双口 BRAM、signal 存储体配双进程规避了读写顺序问题。所有满空判定与指针管理都留给上层 FIFO，存储体只做最纯粹的「按址写 / 按址读」。

> **待本地验证**：以上测试台是骨架，读回校验的时序对齐需要你在本地仿真器里实际跑通并完善；具体波形与 `check_equal` 结果以本地运行为准。

## 6. 本讲小结

- **双时钟双口 RAM = 读写两口分别由两个独立时钟驱动**：与 `dual_port_ram` 的「单 `sys_clk`、两口」不同，本模块写口只看 `write_clk`、读口只看 `read_clk`，两个时钟彼此完全异步。
- **存储体是 signal、声明在 architecture 区**：因为读写分处两个不同进程（不同时钟域），不存在「同周期同地址读写」问题，所以用 signal 即可，**不需要** u6-l2 那套 variable + 顺序控制。
- **两个独立进程 + 单写者单读者**：`write_process` 是 `ram_reg` 的唯一驱动者，`read_process` 只读不写——这是 VHDL 描述「真双口 RAM」的标准结构。
- **寻址用 `std_ulogic_vector` + `unsigned()` + `to_integer`**：地址端口是 `std_ulogic_vector`，寻址前必须先用 `numeric_std` 的 `unsigned()` 解释成无符号数，再转整数下标。
- **极简端口是刻意的**：没有复位、没有 `en`、没有 `read_enable`——流量控制、满空判定、指针管理全部留给上层 FIFO，本模块只当一块「哑存储」。
- **它是异步 FIFO 的存储底座**：`fifo_async`（u9）和 `fifo_sync` 都例化了它；前者用真双时钟，后者把两时钟都接成 `sys_clk`，一份原语服务两种 FIFO。

## 7. 下一步学习建议

本讲把「存储原语」这条线收尾了。下一步有两个方向：

1. **进入第 8 单元「时钟域跨域同步器」**：本讲反复提到「指针的跨域同步」「格雷码」，这些正是 `ff_synchroniser` / `ff_synchroniser_vector` 的职责。先学它们，你才能理解异步 FIFO 如何安全地把写指针送到读域、把读指针送到写域。
2. **进入第 9 单元「FIFO 设计」**：尤其是 u9-l3「异步 FIFO 与格雷码指针」，它会把你今天学到的「哑存储体」与第 8 单元的「同步器」拼成完整的 Cummings 异步 FIFO。学完那一刻，你会清楚地看到：`dual_clock_dual_port_ram` 在整张图里，就是那个安静地待在叶子节点、只管存取的存储砖块。

顺带建议：把本讲综合实践里那个最小测试台真的搭起来跑通（哪怕只在一个时钟频率下先验证读写正确性，再加第二个不同频率的时钟）。亲手看到「两个异步时钟能正确读写同一块存储」，是建立跨时钟域直觉的关键一步。
