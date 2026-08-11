# 双口 RAM 与读写顺序

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚「双口 RAM（dual-port RAM）」与上一讲的「单口 RAM」在端口结构上的根本区别：读、写各拥有一套独立的地址与使能。
- 理解为什么本模块把存储体 `ram_reg` 声明成 VHDL 的 **variable（变量）** 而不是 signal（信号），以及这一选择如何决定「同一时钟周期、同一地址上既读又写」时返回的是新值还是旧值。
- 读懂源码注释里那句警告——「不要把写块移到读块之上，否则会产生读写冒险」——背后的 VHDL 语义与硬件含义，并能亲自用测试台复现它。
- 能够基于现有测试台 `tb_dual_port_ram.vhd` 构造一个同周期同地址读写的用例，观察并解释输出。

## 2. 前置知识

在进入本讲前，请先确认你熟悉以下概念（它们大多已在 u6-l1 建立）：

- **RAM 的基本读写**：RAM 是一个「按地址访问」的存储阵列，写入是把数据放进某个地址，读出是从某个地址取出数据。地址决定访问哪一个存储单元。
- **同步时序逻辑与时钟进程**：本库的 RAM 都是对 `sys_clk` 敏感的进程，真正的读写动作发生在 `rising_edge(sys_clk)`（时钟上升沿）。
- **非约束端口 + 内部推导存储类型**：entity 把 `address`、`write_data`、`read_data` 声明为不带范围的非约束类型，真实位宽推迟到例化时刻由外部连线决定；architecture（本讲里是进程）内部再用属性反推存储深度与数据位宽。这是 u6-l1 已建立的范式，本讲沿用。
- **signal 与 variable 的差别（本讲的重点之一）**：这是本讲会重点展开的新概念，先记住一句口诀——
  - signal 用 `<=` 赋值，更新被「推迟」到进程挂起之后（一个 delta 之后）；
  - variable 用 `:=` 赋值，更新是「立即」的，进程里紧接着的下一行就能读到新值。
- **读写冒险（hazard）/ 读改写（read-modify-write）**：当同一时刻既读又写同一个存储单元时，如果读写之间存在不该有的相互依赖，就可能产生不确定或非预期的耦合，这就是「冒险」。本讲会把这个词落到具体的波形行为上。

> 本讲是 u6-l1「单口 RAM」的直接续篇。如果你还没读过单口 RAM，建议先看一遍 `single_port_ram.vhd` 再回来，因为本讲会频繁把两者对照。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| [ip/memories/ram/dual_port/dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd) | 设计源码（可综合） | 双口 RAM 主体，本讲的核心。 |
| [ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd) | 测试台（仅仿真） | VUnit 测试台，用 OSVVM 随机化做写满/随机读/禁用三类验证。 |
| ip/memories/ram/single_port/single_port_ram.vhd | 设计源码（对照参考） | 上一讲的单口 RAM，用于对比端口结构与存储体类型。 |
| ip/memories/ram/dual_clock_dual_port_ram.vhd | 设计源码（下一讲） | 双时钟双口 RAM，本讲末尾预告，是异步 FIFO 的存储底座。 |

一句话定位：`dual_port_ram.vhd` 是「同一个时钟、独立的读写两口」的存储原语；它解决的核心问题是——**当读和写可能同时发生时，如何安全、可预期地处理它们的相互影响**。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **双口 RAM：读写端口分离**（模块结构本身）
2. **变量型 ram_reg：variable vs signal**（为什么存储体用变量）
3. **read-before-write 顺序：同周期读写顺序与冒险**（那句注释的真正含义）

---

### 4.1 双口 RAM：读写端口分离

#### 4.1.1 概念说明

上一讲的单口 RAM 只有一个 `address` 和一个方向控制信号 `write_and_not_read`：同一时钟周期里，要么写、要么读，二者互斥。它就像一个「单车道」的仓库，同一时刻只能有一辆车进出。

双口 RAM 给读和写各开了一条「车道」：

- 一套**写口**：`write_address` + `write_enable` + `write_data`；
- 一套**读口**：`read_address` + `read_enable` + `read_data`。

两口共用同一个 `sys_clk`、同一个 `en` 和同一个存储体，但各自有独立的地址。于是出现了单口 RAM 里不可能发生的新场景：**在同一个时钟上升沿，用地址 A 写入新数据的同时，用地址 B 读出数据**；极端情况下，连 A 和 B 都可以是同一个地址。

这就引出本模块（以及本讲）要回答的核心问题：

> 当同一周期、同一地址上「既读又写」时，`read_data` 应该返回**旧值**（写入之前的内容）还是**新值**（本周期刚写入的内容）？

答案是：**取决于存储体用 signal 还是 variable，以及进程里读写语句的先后顺序**。本模块先看结构，4.2、4.3 再分别拆开这两点。

#### 4.1.2 核心流程

双口 RAM 每个时钟上升沿的行为可概括为：

```
上升沿到来：
├── 复位有效 (sys_rst_n = '0')?
│     └── read_data <= 全 'X'（don't-care），不改动存储体
├── en 无效?
│     └── 什么都不做
└── en 有效：
      ├── 读口：read_enable=1 → 把 ram_reg[read_address] 送到 read_data
      └── 写口：write_enable=1 → 把 write_data 写进 ram_reg[write_address]
      （两口各自独立判断，可以同时为 1）
```

注意两点：

1. **读、写是两条独立的 `if`**，不是 `if ... elsif ...`，所以两者可以同时成立——这是「双口」的本质。
2. 读出口 `read_data` 仍然是一个**寄存器输出**（信号），所以读出的数据相对地址会**晚一个时钟周期**出现在端口上（这一点和单口 RAM 一致）。

#### 4.1.3 源码精读

先看 entity 的端口声明：

[dual_port_ram.vhd:L13-L25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L13-L25) —— entity 声明了**分离的读写口**：`write_address` 与 `read_address` 各一份，`write_enable` 与 `read_enable` 各一份。这与单口 RAM 的「单 `address` + 单 `write_and_not_read`」形成直接对照。

```vhdl
entity dual_port_ram is
    port (
        sys_clk: in std_ulogic;
        sys_rst_n: in std_ulogic;
        en: in std_ulogic;
        write_enable: in std_ulogic;
        read_enable: in std_ulogic;          -- 与 write_enable 独立
        write_address: in unsigned;          -- 与 read_address 独立
        read_address: in unsigned;
        write_data: in std_ulogic_vector;
        read_data: out std_ulogic_vector
    );
end entity;
```

> **与单口 RAM 对照**：在 `single_port_ram.vhd` 中，方向只有一个 `write_and_not_read` 信号，还要靠一个额外的组合进程把它解码成内部的 `write_enable` / `read_enable`（见 single_port_ram.vhd 第 35–39 行）。双口 RAM 把这两个使能直接做成 entity 端口、由外部控制，因此**省掉了那个解码进程**，读写也更灵活。

再看进程主体里「en 有效」后的两段：

[dual_port_ram.vhd:L43-L52](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L43-L52) —— `en` 有效后，先执行**读块**，再执行**写块**，两者是两条独立的 `if`，可以同时触发。夹在中间的两行注释正是本讲 4.3 要破解的关键。

```vhdl
elsif en then
    if read_enable then
        read_data <= ram_reg(to_integer(read_address));
    end if;
    -- NOTE: Don't move this block above the read block. 
    --       It will cause a read-before-write hazard.
    if write_enable then
        ram_reg(to_integer(write_address)) := write_data;
    end if;
end if;
```

对比单口 RAM 里的等价片段（single_port_ram.vhd 第 54–60 行）用的是 `if write_enable ... elsif read_enable`——互斥分支，永远不可能同时读写；而这里改成两条并列 `if`，正是「双口」的体现。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试台，确认现有验证覆盖了哪些读写组合，并发现「同周期同地址同时读写」这一关键场景**尚未被测试**。

**操作步骤**：

1. 打开 [tb_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd)，定位到 `test_full_ram` 过程（[L111-L145](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L111-L145)）。
2. 仔细看每个时钟周期里 `write_enable` 和 `read_enable` 的取值：先 `write_enable<='1'; read_enable<='0'` 写一拍，再 `write_enable<='0'; read_enable<='1'` 读一拍。
3. 同样检查 `test_random_addresses`（[L147-L177](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L147-L177)）和 `test_when_ram_deactivated`（[L179-L206](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L179-L206)）。

**需要观察的现象**：在全部三个用例里，`write_enable` 与 `read_enable` 从来没有在同一拍里同时为 `'1'`（`test_when_ram_deactivated` 甚至 `en<='0'` 直接禁用整个 RAM）。

**预期结果**：现有测试台只覆盖了「纯写」「纯读」「禁用」三种情况，**完全没有覆盖「同一拍既读又写、且读写地址相同」这一决定 read-before-write 语义的场景**。这正是 4.3 的实践要补上的缺口——这也说明：哪怕测试全绿，也不代表某个边界行为被验证过。

#### 4.1.5 小练习与答案

**练习 1**：如果把双口 RAM 改回单口 RAM 风格，把读块和写块合并成 `if write_enable then ... elsif read_enable then ...`，会丢失什么能力？

**参考答案**：会丢失「同一周期既读又写」的能力。`elsif` 是互斥的，一旦 `write_enable` 为真就不会再读，因此读、写无法在同拍并行发生，退化回单口行为。

**练习 2**：双口 RAM 的读口 `read_data` 相对 `read_address` 有没有节拍延迟？为什么？

**参考答案**：有，延迟一个时钟周期。因为 `read_data` 是进程里用 `<=` 赋值的信号（寄存器输出），上升沿采样 `read_address` 后，数据要到本 delta 周期结束才更新到 `read_data`，在下一个上升沿才能被外部观察到——这和单口 RAM 一致。

---

### 4.2 变量型 ram_reg：variable vs signal

#### 4.2.1 概念说明

要理解本模块最关键的一行代码——`variable ram_reg: ram_t;`——必须先把 VHDL 里 signal 与 variable 的差别讲透。这是本讲的「地基」。

**signal（信号）**

- 用 `<=` 赋值。
- 赋值不是立刻生效的：进程把「下一次的新值」排进一个更新队列，等进程挂起（挂到 `wait`、或进程结束）之后，在一个 **delta 周期**之后才真正更新。
- 因此，在**同一个进程执行过程**中，如果你先 `sig <= 5;`，紧接着读 `sig`，读到的还是**旧值**。
- signal 用于描述「硬件连线 / 寄存器」，跨越进程、跨越时间。

**variable（变量）**

- 用 `:=` 赋值。
- 赋值**立即生效**：下一行读它，读到的就是新值。
- variable 是「顺序执行的临时状态」，只在声明它的进程（或子程序）内部可见。
- 在时钟进程里用 variable 存数据，意味着：**进程内语句的先后顺序会直接影响「读到的是更新前还是更新后的值」**。

一句话对比：

| 特性 | signal `<=` | variable `:=` |
| --- | --- | --- |
| 生效时机 | 进程挂起后的 delta 周期 | 立即，下一行即可见 |
| 同一进程内先后语句是否相互影响 | 否（都读到旧值） | **是**（顺序决定结果） |
| 可见范围 | 全 architecture / 跨进程 | 仅所在进程 / 子程序 |
| 典型用途 | 连线、寄存器输出 | 顺序逻辑的中间状态、本模块的存储体 |

**为什么双口 RAM 要用 variable 存存储体？** 因为它想让「同周期同地址读写」的行为**可由代码顺序精确控制**：读块在前，读到旧值；写块在前，读到新值。如果用 signal 存 `ram_reg`，由于 signal 更新被推迟到 delta 之后，本拍写入的值无论如何都不会被本拍的读看见，顺序也就失去了意义（下一小节会展开）。

> **与单口 RAM 的关键对照**：`single_port_ram.vhd` 第 33 行是 `signal ram_reg: ram_t;`——用 **signal** 存。这没问题，因为单口 RAM 的读写本就互斥，永远不会同拍同地址读写，signal 的「延迟更新」反而正好；并且 `RAM_DEPTH`、`ram_t` 等声明放在 **architecture** 的说明区。而 `dual_port_ram.vhd` 第 33 行改成 `variable ram_reg: ram_t;`，相应地，`RAM_DEPTH`/`ram_depth_t`/`ram_t` 这些声明被移进了**进程的说明区**（第 30–32 行）——因为 variable 必须在声明它的进程内。

#### 4.2.2 核心流程

存储体类型与变量的声明流程：

```
进程说明区（process 与 begin 之间）：
├── constant RAM_DEPTH := 2 ** write_address'length     -- 由写地址位宽推导深度
├── subtype ram_depth_t is natural range 0 to RAM_DEPTH-1
├── type ram_t is array (ram_depth_t) of write_data'subtype  -- 元素位宽取自写数据
└── variable ram_reg : ram_t                              -- 变量型存储体（立即更新）
```

设写地址位宽为 \(N\)，则：

\[
\text{RAM\_DEPTH} = 2^{N}
\]

例如测试台里 `ADDRESS_WIDTH = 8`（见 [tb_dual_port_ram.vhd:L49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L49)），故 RAM_DEPTH = 256，数据位宽由 `DATA_WIDTH = 8` 决定，即一块 256×8 的 RAM。

#### 4.2.3 源码精读

[dual_port_ram.vhd:L29-L35](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L29-L35) —— 在进程说明区推导存储类型，并用 **variable** 声明存储体。这是本模块最核心的几行。

```vhdl
mem_operation_proc: process (sys_clk)
    constant RAM_DEPTH: positive := 2**write_address'length;
    subtype ram_depth_t is natural range 0 to RAM_DEPTH - 1;
    type ram_t is array (ram_depth_t) of write_data'subtype;
    variable ram_reg: ram_t;
begin
    assert RAM_DEPTH <= natural'high report "ADDRESS_WIDTH exceeds the maximum allowed value!" severity error;
```

要点逐条解释：

- **非约束端口 + 内部推导**：`write_address'length` 给出地址位宽，`2**` 算出深度；`write_data'subtype` 取写数据的子类型作为数组元素类型。这套写法和 u6-l1 的单口 RAM 完全一致，让一份源码服务任意位宽。
- **声明位置在进程内**：因为 `ram_reg` 是 variable，而 variable 只能在进程（或子程序）里声明，所以连带着 `RAM_DEPTH`/`ram_depth_t`/`ram_t` 也搬进了进程说明区。对比 single_port_ram 把它们放在 architecture 说明区——这是「signal vs variable」在源码结构上留下的指纹。
- **断言保护**：`assert RAM_DEPTH <= natural'high` 防止地址位宽过大导致 \(2^N\) 整数溢出（与单口 RAM 同样的防护）。

再看写口用的是 `:=`（变量赋值）：

[dual_port_ram.vhd:L49-L51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L49-L51) —— 写口对 `ram_reg` 用 `:=` 立即更新。

```vhdl
if write_enable then
    ram_reg(to_integer(write_address)) := write_data;
end if;
```

而读口对输出 `read_data` 仍用 `<=`（信号赋值）：

[dual_port_ram.vhd:L44-L46](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L44-L46)

```vhdl
if read_enable then
    read_data <= ram_reg(to_integer(read_address));
end if;
```

所以本模块里**两种赋值并存**：

- `ram_reg` 是 variable，`:=` 立即更新——决定「读到的是新值还是旧值」；
- `read_data` 是 signal，`<=` 延迟更新——决定「输出相对地址晚一拍」。

把这两件事分开理解，是读懂 4.3 的前提。

#### 4.2.4 代码实践

**实践目标**：亲手验证「variable 立即更新、signal 延迟更新」，建立对 `:=` 与 `<=` 的直觉。

**操作步骤**（源码阅读 + 在你自己的临时 scratch 测试台里验证）：

1. 在 `dual_port_ram.vhd` 的写块（第 50 行）和读块（第 45 行）之间，**心算**一次执行：先执行读块，此时 `ram_reg` 还没被本拍写改过，所以 `read_data` 拿到的是旧值；再执行写块，`ram_reg` 立即被更新。
2. （可选）写一个 5 行的最小进程实验：声明一个 `variable v : natural := 0;` 和一个 `signal s : natural;`，在时钟进程里先 `v := v + 1; s <= v;`，再用 `report` 打印 `v` 和 `s`。**待本地验证**：你会看到 `v` 立刻是 1，而 `s` 要到下一拍才变成 1。

**需要观察的现象**：variable 的改变在同一次进程执行内可见；signal 的改变要等一个 delta。

**预期结果**：这恰好解释了为什么双口 RAM 必须用 variable——只有立即更新，才让「读块在前 / 写块在前」这两种顺序产生不同的同周期读写结果。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ram_reg` 从 variable 改成 signal（`signal ram_reg: ram_t;`，并把写口改成 `ram_reg(...) <= write_data;`），那么同周期同地址读写时，读会拿到新值还是旧值？顺序还重要吗？

**参考答案**：会拿到**旧值**，而且**读写顺序不再重要**。因为 signal 的更新被推迟到 delta 之后，无论写块在前还是在后，本拍写入的值都不会被本拍的读看见。这也正是单口 RAM 能放心用 signal 的原因（它根本不会同拍同地址读写）。代价是你失去了「让读拿到新值」的能力。

**练习 2**：为什么 `RAM_DEPTH`、`ram_t` 这些声明在双口 RAM 里出现在进程说明区，而在单口 RAM 里出现在 architecture 说明区？

**参考答案**：因为双口 RAM 的存储体 `ram_reg` 是 variable，VHDL 规定 variable 只能在进程/子程序内声明；为了让 `ram_reg : ram_t` 能引用 `ram_t`，`ram_t` 及其依赖的 `RAM_DEPTH`、`ram_depth_t` 也必须跟着进入进程说明区。单口 RAM 用 signal，signal 在 architecture 区声明，类型自然也放在 architecture 区。

---

### 4.3 read-before-write 顺序：同周期读写顺序与冒险

#### 4.3.1 概念说明

现在破解源码里那两行注释：

> `-- NOTE: Don't move this block above the read block.`
> `--       It will cause a read-before-write hazard.`

它的意思是：**不要把「写块」挪到「读块」之上（即改成先写后读），否则会产生读写冒险**。

要理解这句话，需要先建立「读先于写 / 写先于读」两种行为的术语：

- **读先于写（read-first / 读旧值）**：同一周期同地址读写时，读返回**写入之前**的旧内容。读口与写口彼此独立，互不影响——这是「真正的双口」所期望的行为。
- **写先于写读（write-first / 读新值，也叫穿透）**：同一周期同地址读写时，读返回**本周期刚写入**的新内容，相当于写数据「穿透」到读输出。

结合 4.2 学到的「variable 立即更新」，两种顺序的实际行为是：

| 进程内顺序 | 同周期同地址读到的值 | 行为名称 |
| --- | --- | --- |
| 读块在前，写块在后（**当前代码**） | 旧值 | 读先于写（read-first） |
| 写块在前，读块在后（注释禁止） | 新值 | 写先于读（write-first / 穿透） |

**「冒险」到底险在哪里？**

1. **破坏了双口的独立性**。双口 RAM 的卖点就是读、写两条独立通道。一旦写成 write-first，读输出 `read_data` 就会在同一拍里依赖 `write_data`，形成一条本不该存在的「同周期数据通路」`write_data → read_data`。两个本应独立的口被耦合在一起，这就是「读写冒险」。
2. **难以映射到标准 BRAM 的读改写模式**。Xilinx/Intel 的片上 BRAM 原语通常显式支持 read-first、write-first、no-change 三种「读改写（read-during-write）」模式之一；代码顺序必须与目标原语的模式匹配，综合工具才能把它推断成一块 BRAM 而不是一堆寄存器加 mux。当前代码刻意写成 read-first，正是为了贴合常见的 BRAM 行为。
3. **行为「意外」**。使用者按「双口」的直觉，会默认读到旧值；如果代码悄悄写成 write-first，使用者在同拍同地址时就会拿到一个他没预料到的新值，造成难调试的功能 bug。

所以注释里的警告，本质是：**用 variable 存储体时，语句顺序就是硬件行为；乱动顺序，就是乱改电路的读改写语义，从而引入冒险**。

#### 4.3.2 核心流程

用伪代码把两种顺序画清楚（设 `read_address = write_address = A`，且 `read_enable = write_enable = 1`）：

**当前代码：读块在前**

```
进入上升沿：
  执行读块：read_data <= ram_reg[A]     # ram_reg[A] 仍是旧值 → 读到旧值
  执行写块：ram_reg[A] := write_data    # 现在才更新（本拍读已结束）
结果：read_data = 旧值（read-first）
```

**被禁止的改法：写块在前**

```
进入上升沿：
  执行写块：ram_reg[A] := write_data    # 立即更新！
  执行读块：read_data <= ram_reg[A]     # ram_reg[A] 已是新值 → 读到新值
结果：read_data = 新值（write-first / 穿透）→ 冒险
```

关键点：因为 `ram_reg` 是 variable，写块的 `:=` 立即生效，所以「写块在前」会让随后的读块读到刚写入的新值。这就是顺序之所以要紧的全部原因。

#### 4.3.3 源码精读

[dual_port_ram.vhd:L37-L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port_ram.vhd#L37-L53) —— 整个时钟进程，注意读块（先）与写块（后）的相对位置，以及它们之间的警告注释。

```vhdl
if rising_edge(sys_clk) then
    if sys_rst_n = '0' then
        -- NOTE: To infer Xilinx's BRAM, for Intel I don't know ...
        read_data <= (read_data'range => '-');
    elsif en then
        if read_enable then
            read_data <= ram_reg(to_integer(read_address));   -- ① 读块：先执行，读旧值
        end if;
        -- NOTE: Don't move this block above the read block. 
        --       It will cause a read-before-write hazard.
        if write_enable then
            ram_reg(to_integer(write_address)) := write_data; -- ② 写块：后执行，立即更新
        end if;
    end if;
end if;
```

另请留意复位分支（[L38-L42](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/dual_port_ram.vhd#L38-L42)）：复位时只把 `read_data` 置成全 `'-'`（don't-care），**并不清空 `ram_reg`**。注释解释这是为了引导综合工具把 `ram_reg` 推断成 Xilinx BRAM——整块 RAM 无法在一拍内复位，所以干脆不清，省资源。这和单口 RAM（复位分支写 `null;`）是同一思路，只是双口 RAM 顺便把输出置成了 don't-care。

#### 4.3.4 代码实践

这是本讲最重要的动手环节：**亲手复现「读旧值」与「读新值」的差异，并理解它就是注释所说的冒险。** 分两步。

> 说明：本实践需要你**临时修改设计源码**来观察行为差异。请在一个本地副本或 git 工作区里操作，验证完务必还原，不要把改动提交进仓库。

**第 1 步：构造一个「同周期同地址同时读写」的用例（不改设计）**

**实践目标**：补上 4.1.4 发现的测试缺口，验证当前代码在同周期同地址下返回**旧值**。

**操作步骤**：

1. 复制 `tb_dual_port_ram.vhd` 为一个临时测试台（例如 `tb_dual_port_ram_rdwr.vhd`，entity 同名改名），仿照它的骨架。
2. 在 `checker` 进程里新增一个 `run("test_read_write_same_addr")` 用例，逻辑要点：
   - 先 `restart_module` 复位；
   - 令 `en <= '1'`；
   - 向地址 `0` 预先写一个已知值（比如 `x"AA"`），方法参考 `test_full_ram`：`write_enable<='1'; read_enable<='0'; write_address<=0; data_in<=x"AA"; wait_sys_clk_cycles(1);`
   - 然后**在同一拍**同时置 `write_enable<='1'; read_enable<='1'; write_address<=0; read_address<=0; data_in<=x"55";`，并 `wait_sys_clk_cycles(1);`
   - 此时 `data_out` 是上一拍（同周期读写那一拍）采样得到的结果。
3. 用 `check_equal` 或 `info` 打印 `data_out`。

**需要观察的现象 / 预期结果**：当前代码（读块在前）下，同周期同地址读到的是**旧值** `x"AA"`，而不是刚写入的 `x"55"`。这正是 read-first 行为。

**第 2 步：交换读写顺序，复现冒险（临时改设计）**

**实践目标**：把写块挪到读块之上，观察行为变成 write-first（读新值），体会注释警告的「冒险」。

**操作步骤**：

1. 打开 `dual_port_ram.vhd`，把写块（第 49–51 行）整体剪切到读块（第 44–46 行）**之前**。改动后的 `elsif en` 段应当形如（**示例代码：你将临时改成的样子**，并非仓库原有代码）：

   ```vhdl
   -- ⚠ 示例代码：临时改写，用于复现冒险，验证后请还原
   elsif en then
       if write_enable then
           ram_reg(to_integer(write_address)) := write_data;   -- 写块被挪到前面
       end if;
       if read_enable then
           read_data <= ram_reg(to_integer(read_address));     -- 读块现在在后
       end if;
   end if;
   ```

2. 重新编译并运行你第 1 步写的 `test_read_write_same_addr` 用例。

**需要观察的现象 / 预期结果**：同周期同地址读写时，`data_out` 变成**新值** `x"55"`——因为写块先用 `:=` 立即更新了 `ram_reg[0]`，读块随后读到新值。这就是 write-first 穿透，也就是注释所说「把写块移到读块之上」会引入的耦合/冒险。验证完，**务必把源码还原**。

**记录差异**：用一张小表对比两种顺序：

| 读写顺序 | `data_out`（同周期同地址） | 行为 | 是否冒险 |
| --- | --- | --- | --- |
| 读块在前（原代码） | `x"AA"`（旧值） | read-first | 否 |
| 写块在前（临时改动） | `x"55"`（新值） | write-first / 穿透 | 是 |

> **待本地验证**：以上预期来自 VHDL 的 variable 立即更新语义，结论是确定的；但具体仿真器输出与波形需要你在本地跑一遍确认。

#### 4.3.5 小练习与答案

**练习 1**：注释说「不要把写块移到读块之上」，可如果设计者其实想要的就是 write-first 行为（同周期同地址读新值），能不能就放心地把写块挪到前面？

**参考答案**：语法上可以，但要同时承担两个后果：(1) 读输出会在同周期依赖写输入，读、写两口不再独立，使用方必须知道这一点；(2) 综合时该 RAM 不一定能被推断成单块标准 BRAM（取决于目标器件原语支持哪种读改写模式），可能变成寄存器 + mux，面积/时序变差。注释的潜台词是：本库的端口契约默认 read-first，改动顺序就破坏了这个契约。

**练习 2**：假如把 `ram_reg` 改回 signal（如 4.2.5 练习 1），那么「写块在前」还会引发同样的冒险吗？

**参考答案**：不会引发 write-first 穿透。因为 signal 的更新推迟到 delta 之后，本拍写入的值本拍读不到，无论顺序如何都读到旧值（恒为 read-first）。但这并不意味着「用 signal 就更安全」——它只是把顺序的影响「屏蔽」了，同时也丢失了实现 write-first 的能力。本库选择 variable + 固定顺序，是为了**精确、可控地**表达 read-first 语义，并贴合 BRAM 推断。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个贯穿任务：

**任务**：为 `dual_port_ram` 编写一个完整的 VUnit 用例 `test_read_during_write`，专门验证「同周期同地址读写」的 read-first 语义，并保证它能被 `test_runner.py` 自动发现。

要求：

1. **沿用现有测试台骨架**：参考 [tb_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd) 的 `main` + `checker` 双进程结构、`generate_advanced_clock`（[L67](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L67)）、`wait_sys_clk_cycles`、`restart_module` 等过程（[L97-L109](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L97-L109)）。
2. **测试内容**：
   - 预先向地址 `5` 写入 `x"3C"`；
   - 在同一拍对地址 `5` 同时读（`read_enable='1'`）和写（`write_enable='1'`，`data_in=x"C3"`），`read_address=write_address=5`；
   - 断言此时读出的是旧值 `x"3C"`（read-first）；
   - 下一拍再单独读地址 `5`，断言读到的是上一拍写入的新值 `x"C3"`。
3. **回归保护**：把该用例加进 `test_suite` 循环（仿照 [L213-L223](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L213-L223)）。如果以后有人误把写块挪到读块之前，这个用例应当**失败**——它就成了 read-first 语义的「看门狗」。
4. **验证**：按 u1-l3 学到的方式跑 `test_runner.py`，确认新用例被发现且通过；再临时交换读写顺序，确认该用例转红，最后还原源码。

> 提示：如果 VUnit 报 `check_equal` 比对失败，先核对 `wait_sys_clk_cycles` 内部那个 `PROPAGATION_TIME`（[L34](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_port/tb/tb_dual_port_ram.vhd#L34)，1 ns）是否给够了输出寄存器的建立时间。

## 6. 本讲小结

- **双口 RAM = 读写两口分离**：与单口 RAM 的「单地址 + 单方向信号」不同，`dual_port_ram` 给读、写各一套独立的地址和使能，两口可以同拍并行工作。
- **存储体用 variable**：`variable ram_reg` 配合 `:=` 立即更新，使进程内语句顺序能精确控制「同周期同地址读写」的结果；这与单口 RAM 用 `signal ram_reg` + `<=` 形成对照，也导致类型声明被搬进了进程说明区。
- **读写顺序决定读改写语义**：读块在前 → read-first（读旧值，当前代码）；写块在前 → write-first（读新值，穿透）。
- **那条注释警告的是「乱改顺序」**：因为 variable 让顺序等价于硬件行为，把写块挪到读块之上会引入同周期 `write_data → read_data` 的耦合（冒险），并可能破坏 BRAM 推断与默认的 read-first 端口契约。
- **复位不清存储体**：复位只把 `read_data` 置 don't-care、不动 `ram_reg`，目的是引导综合工具把存储体推断成片上 BRAM（与单口 RAM 同思路）。
- **现有测试台有盲区**：三个用例从不同时置 `read_enable` 与 `write_enable` 为 1，因此 read-during-write 行为未被覆盖——本讲的综合实践正是来补这个缺口的。

## 7. 下一步学习建议

本讲的 `dual_port_ram` 是「**同一时钟**、两口」的存储。下一步推荐学习 **u6-l3 双时钟双口 RAM**（`dual_clock_dual_port_ram.vhd`）：

- 它把写口交给 `write_clk`、读口交给 `read_clk`，两个时钟彼此异步——这正是**异步 FIFO** 的存储底座。
- 学完它，你就能进入第 9 单元「FIFO 设计」，理解异步 FIFO 如何在这块双时钟双口 RAM 之上，用格雷码指针 + 跨域同步器（u8）实现安全的跨时钟域数据搬运。

顺带建议：把本讲综合实践里那个 `test_read_during_write` 看门狗用例真的写出来并跑通——它会让你对「variable + 顺序 = 硬件行为」这句话形成肌肉记忆，这是读后续所有时序模块的基础。
