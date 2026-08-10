# 同步 FIFO 的行为级实现（own_behavioural_sync_fifo）

## 1. 本讲目标

本讲精读 HDL-Core-Library 中**同步 FIFO** 的「自研行为级」实现 `own_behavioural_sync_fifo`。它是整个 FIFO 家族里最纯粹、最适合用来理解 FIFO 内部机理的一份源码，也是后续异步 FIFO（u9-l3 / u9-l4）的前置基础。

学完本讲你应该能够：

- 说清「写指针 / 读指针 / 填充水位（fifo_fill_level）」三者如何共同驱动 `full`、`empty`、`words_stored`。
- 理解同步 FIFO 为何可以用**一个统一的填充水位计数器**来判定满空，而不必像异步 FIFO 那样用格雷码指针比较（这是 u9-l3 的伏笔）。
- 看懂「同一周期同时读写时填充水位不变」的分支处理，以及写满 / 读空时请求如何被屏蔽。
- 看懂被 `-- synthesis off` 包裹、由 `UNDER_AND_OVERFLOW_ASSERTIONS` 开关控制的断言块，为何它只参与仿真、不进入综合。
- 会用测试台 `tb_fifo_sync` 验证上述行为，并能动手开启断言复现「满写 / 空读」告警。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是 FIFO。** FIFO（First-In First-Out，先进先出队列）是一种「按到达顺序缓存数据」的存储结构：先写进去的数据先被读出来。它解决的是「生产者与消费者速率不匹配」的问题——当写入方短暂地比读出方快时，FIFO 把来不及处理的数据暂存起来；当读出方追上来时再吐出。FIFO 的核心状态量有两个：**满（full）**表示「不能再写了，再写就溢出」，**空（empty）**表示「不能再读了，再读就欠读（underflow）」。

**同步 FIFO 与异步 FIFO 的区别。** 「同步」指写端口和读端口共享**同一个时钟** `sys_clk`。因为两边在同一个节拍下动作，我们可以放心地用一个寄存器 `fifo_fill_level` 来记录「FIFO 里现在有几个字」，满空标志直接由它推导。而异步 FIFO（u9-l3）的写时钟与读时钟不同，写域无法直接读到读域的水位，必须把指针用格雷码跨时钟域传过去再比较——那是更复杂的话题。本讲只聚焦「单时钟 + 一个水位计数器」这条最清晰的路径。

**填充水位法相比指针比较法的优势。** 因为满空完全由 `fifo_fill_level` 这一个计数器决定，所以 `FIFO_DEPTH` **不必是 2 的幂**也能正确工作（指针仍按 \(2^{\text{ADDR\_WIDTH}}\) 折回，但容量由水位封顶）。异步 FIFO 用指针比较判满空时则要求深度必须是 2 的幂。这是本模块一个值得记住的设计取舍。

> 承接：本讲假设你已读过 u2-l1（同一 entity 多架构）、u3-l2（`utils_pkg` 的 `to_bits`）、u6-l3（`dual_clock_dual_port_ram`）。本讲的存储底座正是 u6-l3 那块「哑存储」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读重点 |
|------|------|--------------|
| [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) | 设计源码：同步 FIFO，含三套架构 | `own_behavioural_sync_fifo` 架构（147–241 行） |
| [tb_fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd) | VUnit 测试台，9 个用例 | 同时例化 xilinx 与 own 两套 DUT 做等价回归 |
| [dual_clock_dual_port_ram.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/ram/dual_clock_dual_port_ram.vhd) | 存储底座（u6-l3 讲过） | 被 sync FIFO 以「双时钟都接 sys_clk」复用 |
| [async_fifo.drawio.svg](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/docs/async_fifo.drawio.svg) | draw.io 架构图（标注为异步 FIFO） | 直观展示「写域 / 读域 + 共享双口 RAM」的结构，本讲的同步版本相当于把两个时钟域合并 |

> 提醒：`async_fifo.drawio.svg` 标题是 `custom_dual_clock_fifo`，画的是**异步** FIFO 的结构。它对理解本讲仍有帮助——它把「写指针域 / 读指针域 / 中间那块共享双口 RAM」画得很清楚，本讲的同步 FIFO 就是把图里的两个时钟域合并成一个 `sys_clk` 域。

---

## 4. 核心概念与源码讲解

### 4.1 own_behavioural_sync_fifo：模块全貌与数据通路

#### 4.1.1 概念说明

`fifo_sync` 这个 entity 同时提供三套架构（承接 u2-l1）：

- `xilinx_behavioural_sync_fifo` —— 封装 Xilinx `xpm_fifo_sync` 原语（u9-l2 详讲）。
- `intel_behavioural_sync_fifo` —— 封装 Intel `scfifo` 原语（u9-l2 详讲）。
- `own_behavioural_sync_fifo` —— **厂商无关的行为级实现**，不依赖任何厂商库，可开箱仿真。**本讲只讲这一套。**

「自研行为级」的价值在于：它让你在没有任何 FPGA 厂商工具的情况下，仅凭一个开源仿真器就能跑通、调试、理解 FIFO 的全部行为；同时它也是另外两套厂商封装的**行为参照物**——测试台正是拿它和 xilinx 实现并排对比来做等价性回归。

#### 4.1.2 核心流程

整个模块可以拆成「控制通路」和「数据通路」两条线：

```text
                 ┌──────────── 控制通路（决定写不写、读不读、水位多少）────────────┐
 write_enable ──┐                                                               │
 read_enable ──┐│   ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
 full, empty ─►├──►│ mem_req_proc │───►│ fifo_ctrl_   │───►│ write_pointer    │  │
               ││  │ (组合,屏蔽)  │    │ proc(时序)   │    │ read_pointer     │  │
               ││  └──────────────┘    └──────────────┘    │ fifo_fill_level  │  │
               │└─────────────────────────────────────────► │                  │  │
               │                            ┌───────────────│ full/empty/      │  │
               │                            │  组合(mem_    │ words_stored     │  │
               │                            │  status_proc)│                  │  │
               │                            └───────────────┴──────────────────┘  │
                 └────────────────────────────────────────────────────────────────┘
                 ┌──────────── 数据通路（真正搬数据）────────────┐
 write_data ──────────────►┌──────────────────────┐───► read_data
 write_address(写指针)────►│ dual_clock_dual_     │◄─── read_address(读指针)
 read_address(读指针)─────►│ port_ram (u6-l3)     │
                           │  write_clk=read_clk  │
                           │       = sys_clk      │
                           └──────────────────────┘
 read_enable, empty ──(时序)──► read_data_valid
```

读法：控制通路算出「这一拍到底要不要写、要不要读」，更新两个指针和一个水位；数据通路则用这两个指针去寻址那块双口 RAM，完成真正的数据搬运。两条通路靠 `write_pointer` / `read_pointer` 这两个共享信号耦合。

#### 4.1.3 源码精读

先看 entity（端口契约三套架构共用）：

[fifo_sync.vhd:7-25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L7-L25) —— 声明 `fifo_sync` 的 generic 与端口。

几个要点：

- `FIFO_DEPTH: positive := 2` 是默认深度；测试台用的是 `1024`。
- `UNDER_AND_OVERFLOW_ASSERTIONS: boolean := false` 默认**关闭**，是 4.3 节的主角。
- `write_data` / `read_data` 是**非约束** `std_ulogic_vector`，位宽推迟到例化时由外部连线决定（同 u6-l1 的套路），所以一份源码服务任意数据位宽。
- `words_stored: out natural range 0 to FIFO_DEPTH` —— 输出当前存了几个字，范围被严格约束在 `[0, FIFO_DEPTH]`。

再看 `own_behavioural_sync_fifo` 的架构头与关键常量：

[fifo_sync.vhd:147-158](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L147-L158) —— 架构说明区：算地址位宽、声明指针与水位。

最关键的一行是地址位宽的推导：

```vhdl
constant ADDR_WIDTH: natural := to_bits(FIFO_DEPTH - 1);
```

承接 u3-l2，`to_bits(n)` 返回表示 `n` 所需的最少位数（约 \( \lceil \log_2(n+1) \rceil \)）。取 `FIFO_DEPTH - 1` 是因为地址要能覆盖 `0 .. FIFO_DEPTH-1` 这个范围。例如 `FIFO_DEPTH = 1024` 时 `to_bits(1023) = 10`，指针是 10 位，寻址空间正好 `2^10 = 1024` 个槽。

> 注意这里取的是 `to_bits(FIFO_DEPTH - 1)`（地址位宽），而 4.2 节的水位计数器位宽取的是 `to_bits(FIFO_DEPTH)` 量级。承接 u3-l2 提到的「地址位宽与计数位宽差 1」在这里再次出现。

最后看数据通路如何复用 u6-l3 的双口 RAM：

[fifo_sync.vhd:231-240](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L231-L240) —— 例化 `dual_clock_dual_port_ram`。

```vhdl
dual_port_ram_inst: entity work.dual_clock_dual_port_ram
    port map (
        write_clk    => sys_clk,        -- ← 关键：写时钟
        ...
        read_clk     => sys_clk,        -- ← 关键：读时钟也是 sys_clk
        ...
    );
```

这就是「同步 FIFO 复用双时钟双口 RAM」的真相：把那块为异步 FIFO 设计的、读写分属不同时钟的 RAM，**两个时钟端口都接同一个 `sys_clk`**，于是它退化成一块单时钟双口 RAM。一份存储原语同时服务同步与异步两种 FIFO，这正是 u6-l3 把它设计成「无复位、无使能的哑存储」的回报——所有策略都由上层 FIFO 负责。

注意 RAM 的 `write_enable` 接的不是用户的 `write_enable`，而是 `fifo_write_request`（经过屏蔽的写请求，4.2 节详解）；RAM 没有读使能端口，每拍都无条件读出 `read_address` 处的数据（u6-l3 已讲），所以模块另用一个 `read_data_valid` 标志告诉外部「这一拍的 `read_data` 是不是一次有效读取」。

#### 4.1.4 代码实践：例化 own_behavioural_sync_fifo

**实践目标：** 用直接例化语法选中「自研行为级」架构，并理解它为何不需要任何厂商库。

**操作步骤：**

1. 在一个最小测试台骨架里，按下文方式例化（这是「示例代码」，不是项目原有文件）：

```vhdl
-- 示例代码：最小例化
signal clk, rst_n, wr, rd, full, empty, rdv : std_ulogic;
signal wdata : std_ulogic_vector(7 downto 0);
signal rdata : std_ulogic_vector(7 downto 0);
signal cnt   : natural range 0 to 16;

dut : entity work.fifo_sync(own_behavioural_sync_fifo)   -- ← 选定架构
    generic map (
        FIFO_DEPTH                   => 16,
        UNDER_AND_OVERFLOW_ASSERTIONS => true              -- 本练习先不开也行
    )
    port map (
        sys_clk => clk, sys_rst_n => rst_n,
        write_enable => wr, write_data => wdata,
        read_enable  => rd, read_data => rdata,
        read_data_valid => rdv,
        full => full, empty => empty, words_stored => cnt
    );
```

2. 对照本仓库真实测试台的写法 [tb_fifo_sync.vhd:587-602](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L587-L602)，确认你的连线与之一致。

**需要观察的现象：** 因为选的是 `own_behavioural_sync_fifo`，**不需要** `use xpm` / `use altera_mf`，也不需要 `use_xilinx_libs`，仅靠 `use work.utils_pkg.all`（来自 vhdl_utils 子模块，见 u3-l2）即可编译通过。

**预期结果：** 编译通过，无 `glbl.GSR` 之类厂商库报错（对照 u2-l2）。若报 `utils_pkg` 找不到，说明子模块未拉取，需 `git submodule update --init`。

#### 4.1.5 小练习与答案

**练习 1：** 把例化的架构名从 `own_behavioural_sync_fifo` 改成 `xilinx_behavioural_sync_fifo`，不添加任何库声明，会发生什么？

**参考答案：** 会因为缺少 `library xpm; use xpm.vcomponents.all;` 以及预编译的 xpm 仿真库而编译失败（通常表现为 `xpm_fifo_sync` 无法解析，或运行期 `glbl.GSR` 报错）。这正是 u2-l1 强调的「依赖局部化」：厂商库声明紧贴在 `xilinx_behavioural_*` 架构之前，换架构就要换依赖。

**练习 2：** 为什么 RAM 的两个时钟端口都接 `sys_clk` 之后，它就「退化」成了单时钟 RAM？

**参考答案：** 因为此时写进程和读进程对**同一个时钟的同一个上升沿**敏感，两个端口同步动作，不再存在跨时钟域问题；原本为异步 FIFO 准备的「独立读写时钟」能力被收窄成单时钟双口访问。

---

### 4.2 写/读指针 + 填充水位：核心控制逻辑

#### 4.2.1 概念说明

这一节是本讲的灵魂。要理解三件事：

1. **指针会折回（wrap-around）。** 写指针 `write_pointer` 和读指针 `read_pointer` 都是 `ADDR_WIDTH` 位的无符号数，每写一字写指针 `+1`、每读一字读指针 `+1`，到达 \(2^{\text{ADDR\_WIDTH}}-1\) 后回到 0。它们只负责「下一个数据存 / 取在 RAM 的哪个槽」，本身不带满空信息。

2. **满空由「填充水位」`fifo_fill_level` 单独决定。** 这是一个独立维护的计数器，范围 `[0, FIFO_DEPTH]`：

   - 空：\( \text{fifo\_fill\_level} = 0 \)
   - 满：\( \text{fifo\_fill\_level} \ge \text{FIFO\_DEPTH} \)

   水位每写一字 `+1`、每读一字 `-1`、同时读写则**不变**。

3. **请求会被满空屏蔽。** 用户给了 `write_enable` 不代表真写——满的时候写请求被屏蔽；同理空的时候读请求被屏蔽。屏蔽后的信号才叫 `fifo_write_request` / `fifo_read_request`。

为什么同步 FIFO 能这么做、而异步 FIFO 不能？因为同步 FIFO 的写域和读域共享同一个时钟，`fifo_fill_level` 这个寄存器在同一拍对读写两边都可见，可以放心地作为唯一的满空裁判。异步 FIFO 的水位寄存器无法被另一个时钟域安全读取，只能改用「指针跨域 + 格雷码比较」（u9-l3）。

#### 4.2.2 核心流程

一拍之内（从一个 `sys_clk` 上升沿到下一个）的逻辑顺序：

```text
① 当前水位 fifo_fill_level（寄存器，上一拍更新的值）
        │ 组合
        ▼
② mem_status_proc：full  = (fill_level >= FIFO_DEPTH)
                   empty = (fill_level == 0)
        │ 组合
        ▼
③ mem_req_proc：fifo_write_request = write_enable and (not full)
                fifo_read_request  = read_enable  and (not empty)
        │ 时序（在上升沿采样 ③ 的结果）
        ▼
④ fifo_ctrl_proc：按 write_req / read_req 的组合更新指针与水位
        ┌─────────────────┬─────────────┬──────────────────────────────┐
        │ write_req read_req│ 水位变化     │ 指针变化                      │
        ├─────────────────┼─────────────┼──────────────────────────────┤
        │    1         1   │  不变 (±0)   │ 两个指针都 +1                 │
        │    1         0   │  +1          │ 写指针 +1                     │
        │    0         1   │  −1          │ 读指针 +1                     │
        │    0         0   │  不变        │  不动                         │
        └─────────────────┴─────────────┴──────────────────────────────┘
        │
        ▼
⑤ 下一拍：fifo_fill_level 更新 → full/empty/words_stored 组合重算
```

注意「同时读写（write_req=1 且 read_req=1）时水位不变」这一行——它是 FIFO 能在「既不满也不空」的稳态下保持每拍吞吐 1 字的关键：一进一出，净增量为零，水位维持，但两个指针都前进一步，数据持续流动。

#### 4.2.3 源码精读

水位与指针的声明（注意位宽差异）：

[fifo_sync.vhd:150-152](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L150-L152) —— 指针 `ADDR_WIDTH-1 downto 0`，水位 `ADDR_WIDTH downto 0`（多一位）。

```vhdl
signal write_pointer: unsigned(ADDR_WIDTH - 1 downto 0);  -- 折回地址
signal read_pointer : unsigned(ADDR_WIDTH - 1 downto 0);  -- 折回地址
signal fifo_fill_level: unsigned(ADDR_WIDTH downto 0);    -- 多一位，能装下 FIFO_DEPTH
```

为什么水位多一位？指针范围是 \(0 \ldots 2^{\text{ADDR\_WIDTH}}-1\)，而水位要能表示到 `FIFO_DEPTH`（当 `FIFO_DEPTH` 恰为 \(2^{\text{ADDR\_WIDTH}}\) 时需要 `ADDR_WIDTH+1` 位）。这是「计数位宽比地址位宽多一」的体现。

满空的组合推导：

[fifo_sync.vhd:177-181](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L177-L181) —— `mem_status_proc`，由水位组合出 `full`/`empty`。

```vhdl
mem_status_proc: process (all)
begin
    full  <= '1' when fifo_fill_level >= FIFO_DEPTH else '0';
    empty <= '1' when fifo_fill_level = 0           else '0';
end process;
```

`process(all)` 对所有读到的信号敏感，是纯组合逻辑。`full`/`empty` 跟随 `fifo_fill_level`（一个寄存器）组合变化，所以它们在波形上「紧贴时钟沿」出现（沿之后一个 delta 就稳定）。

请求屏蔽：

[fifo_sync.vhd:183-187](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L183-L187) —— `mem_req_proc`，把用户的使能按满空屏蔽成实际请求。

```vhdl
fifo_write_request <= write_enable and not full;   -- 满了就不写
fifo_read_request  <= read_enable  and not empty;  -- 空了就不读
```

这一步是「满写 / 空读被忽略」行为的根源。测试台 `test_edge_cases` 正是利用了这一点（见 4.2.4）。

核心状态机：

[fifo_sync.vhd:189-214](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L189-L214) —— `fifo_ctrl_proc`，时序进程，更新指针与水位。

```vhdl
fifo_ctrl_proc: process (sys_clk)
begin
    if rising_edge(sys_clk) then
        if sys_rst_n = '0' then
            write_pointer <= (others => '0');
            read_pointer  <= (others => '0');
            fifo_fill_level <= (others => '0');
        else
            if fifo_write_request and fifo_read_request then
                -- 同时读写：水位不变，两指针都前进
                write_pointer <= write_pointer + 1;
                read_pointer  <= read_pointer  + 1;
            elsif fifo_write_request then
                write_pointer   <= write_pointer + 1;
                fifo_fill_level <= fifo_fill_level + 1;
            elsif fifo_read_request then
                read_pointer    <= read_pointer  + 1;
                fifo_fill_level <= fifo_fill_level - 1;
            end if;
        end if;
    end if;
end process;
```

注意三个细节：

1. 复位把指针和水位**全部清零**（这与异步 FIFO 不同——异步 FIFO 的复位还会牵涉读指针重放，见 u9-l4）。
2. `if ... and ... then` 的优先级：**同时读写分支在最前**，所以两请求都为真时走「水位不变」分支，不会被后面的「只写」分支抢走。
3. 指针用 `unsigned` 的 `+1`，自动按 \(2^{\text{ADDR\_WIDTH}}\) 折回，无需写 `mod`。

水位转字数：

[fifo_sync.vhd:216](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L216) —— 把水位转成对外输出的 `words_stored`。

```vhdl
words_stored <= FIFO_DEPTH when full else to_integer(fifo_fill_level);
```

`full` 时强制钳位到 `FIFO_DEPTH`，是一种防御性写法——即便水位因任何原因略超 `FIFO_DEPTH`，对外也不会报告越界值，保证 `words_stored` 始终落在声明的 `0 to FIFO_DEPTH` 范围内。

#### 4.2.4 代码实践：跑 tb_fifo_sync，跟踪满空与水位时序

**实践目标：** 用真实测试台验证「写满再读空」「持续同时读写」两种场景下 `full / empty / words_stored / fifo_fill_level` 的时序关系。

**操作步骤：**

1. 按 u1-l3 的方式准备环境（venv + `pip install -r ip/requirements.txt` + `git submodule update --init`），然后用 `ip/test_runner.py` 跑测试台（具体命令以 u1-l3 为准）。
2. 关注两个用例：
   - **写满再读空：** [tb_fifo_sync.vhd:174-211](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L174-L211)（`test_full_fifo`）——先连写 `FIFO_DEPTH=1024` 字把 FIFO 写满，再连读 1024 字读空。
   - **持续同时读写：** [tb_fifo_sync.vhd:387-433](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L387-L433)（`test_simultaneous_read_write_advanced`）——先填入 16 字，然后连续 8 拍「`write_enable=1` 且 `read_enable=1`」。
3. 想看波形的话，对照 [tb_fifo_sync.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.do) 用 ModelSim/QuestaSim 打开；该 `.do` 已经把 `DuT_own` 内部的 `fifo_fill_level / write_pointer / read_pointer / fifo_*_request` 都加进了波形分组（16–20 行）。

**需要观察的现象与预期结果：**

- `test_full_fifo`：写入过程中 `words_stored` 逐拍 `+1`，第 1024 拍 `full_own` 拉高、`empty_own` 为 0；读出过程逐拍 `-1`，第 1024 拍 `empty_own` 拉高。读出的数据与写入顺序一致（先进先出，[tb_fifo_sync.vhd:197-198](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L197-L198)）。
- `test_simultaneous_read_write_advanced`：进入同时读写阶段后，`fifo_fill_level`（波形上）**保持 16 不变**（因为已经先填了 16 字，每拍一进一出），而 `write_pointer` 与 `read_pointer` 同步前进；`words_stored_own` 相应保持 16。
- 时序细节：从 `write_enable` 拉高到 `words_stored` 反映 `+1`，中间隔**一个时钟沿**（因为水位在 `fifo_ctrl_proc` 里是寄存器更新）。这一点已被 [tb_fifo_sync.vhd:356-364](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L356-L364) 的 `test_word_count_accuracy` 断言验证（写 1 拍后立即查 `words_stored = i+1`）。

> 说明：随机化用例（如 `test_stress_operations`）的具体数据因种子而异，但「最终回到空、过程不丢不重」的结论是确定的。具体控制台输出与波形数值以本地实跑为准。

#### 4.2.5 小练习与答案

**练习 1：** 假如把 `fifo_ctrl_proc` 里「同时读写」分支移到最后（写成 `elsif fifo_write_request then ... elsif fifo_read_request then ... elsif (两请求都真) then ...`），会发生什么？

**参考答案：** 「同时读写」分支永远不会被命中，因为前两个 `elsif` 会先分别匹配「只写」「只读」中的某一个（只要有一个请求为真）。结果是同时读写时会被误判为「只写」或「只读」之一，水位错误地 `+1` 或 `-1`，FIFO 容量会越界。所以源码刻意把同时读写分支放在**最前**。

**练习 2：** 为什么 `full` 的判定是 `fifo_fill_level >= FIFO_DEPTH` 用 `>=` 而不是 `=`？

**参考答案：** 这是防御性写法。正常情况下水位最高就是 `FIFO_DEPTH`；但用 `>=` 能容忍水位瞬时略高（例如某些时序边界或未来改动）时仍正确判满，比 `=` 更稳健，且综合代价相同。

---

### 4.3 UNDER_AND_OVERFLOW_ASSERTIONS：只参与仿真的断言

#### 4.3.1 概念说明

工程上，FIFO 在满时被写（overflow，上溢）或在空时被读（underflow，下溢）通常是**使用方的逻辑错误**——本模块在 4.2 节已经通过请求屏蔽保证「满写 / 空读被忽略，不会损坏数据」，但「用户**试图**满写 / 空读」这件事本身值得被报告，方便调试。

`own_behavioural_sync_fifo` 用一段**断言进程**来做这件事，并用两层「开关」确保它**只存在于仿真、不进入综合**：

1. `-- synthesis off` / `-- synthesis on` 注释对：告诉综合工具「夹在这对注释之间的代码不要综合」。这是与具体工具无关的通用约定。
2. `if UNDER_AND_OVERFLOW_ASSERTIONS generate`：编译期的 `generate` 开关，由 generic 控制，默认 `false`。即便在仿真里，也可以把它关掉。

两层叠加 = 默认完全不生效；只有同时「开了 generic」且「在仿真中」才会真正监控。

#### 4.3.2 核心流程

```text
每个 sys_clk 上升沿：
  if (write_enable and full)  →  report "...FIFO is full and being written!"  severity warning
  if (read_enable  and empty) →  report "...FIFO is empty and being read!"    severity warning
```

两个关键点：

- 断言检查的是**用户原始的** `write_enable` / `read_enable`，而不是被屏蔽后的 `fifo_write_request` / `fifo_read_request`。因为我们要抓的是「使用方**试图**违规」这个意图，而非「实际有没有生效」。
- 严重级别是 `warning`（不是 `error`/`failure`），所以仿真**不会**因此中断或判定测试失败，只在日志里打印一行。这让断言成为「只提醒、不挡道」的调试辅助。

#### 4.3.3 源码精读

断言块全文：

[fifo_sync.vhd:159-175](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L159-L175) —— 被 `-- synthesis off` 包裹、由 `UNDER_AND_OVERFLOW_ASSERTIONS` 控制的断言进程。

```vhdl
-- assertion logic for simulation - not synthesised
-- synthesis off
ASSERTION_HINT: if UNDER_AND_OVERFLOW_ASSERTIONS generate
    fifo_overflow_underflow_assertion: process (sys_clk)
    begin
        if rising_edge(sys_clk) then
            if write_enable and full then
                report "Assert Failure - FIFO is full and being written!" severity warning;
            end if;
            if read_enable and empty then
                report "Assert Failure - FIFO is empty and being read!" severity warning;
            end if;
        end if;
    end process;
end generate;
-- synthesis on
```

阅读提示：

- 第 160 行 `-- synthesis off` 与第 175 行 `-- synthesis on` 是「括号」，综合工具会跳过中间所有内容。
- 第 161 行 `if UNDER_AND_OVERFLOW_ASSERTIONS generate` 是编译期裁剪：generic 为 `false` 时，这段 `generate` 在精化期不生成任何硬件，仿真里也不会有这个进程。
- 只有 generic 为 `true`（且在仿真中），这个对 `sys_clk` 敏感的进程才存在，开始监控满写 / 空读。

承接 u11-l2：这种「设计源码里用 `synthesis off` 包裹 `report/assert`」是本库的通用做法，与验证侧测试台里的 `check_equal`（VUnit）分工不同——前者在设计内部做自检，后者在测试台里做结果比对。

#### 4.3.4 代码实践：开启断言，复现满写 / 空读告警

**实践目标：** 让 `UNDER_AND_OVERFLOW_ASSERTIONS` 真正生效，并观察它在「满写 / 空读」时打印的告警。

**操作步骤：**

1. 打开 [tb_fifo_sync.vhd:587-590](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L587-L590)，你会看到 `DuT_own` 的 `generic map` **只设了 `FIFO_DEPTH`**，没有设 `UNDER_AND_OVERFLOW_ASSERTIONS`，所以它默认是 `false`——这就是默认跑测试看不到告警的原因。
2. 在 `DuT_own` 的 `generic map` 里**加一行**（这是学习性质的修改，请在自己的副本上进行）：

```vhdl
DuT_own: entity work.fifo_sync(own_behavioural_sync_fifo)
    generic map (
        FIFO_DEPTH                    => FIFO_DEPTH,
        UNDER_AND_OVERFLOW_ASSERTIONS => true   -- ← 新增这一行
    )
    ...
```

3. 单独跑 `test_edge_cases` 这个用例。它已经构造好了触发条件：
   - 满写：[tb_fifo_sync.vhd:441-455](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L441-L455)（写满后再 `write_enable <= '1'` 持续 3 拍）。
   - 空读：[tb_fifo_sync.vhd:459-471](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L459-L471)（读空后再 `read_enable <= '1'` 持续 3 拍）。

**需要观察的现象：** 仿真日志里应出现（待本地验证具体打印条数）：

```
** Warning: Assert Failure - FIFO is full and being written!
** Warning: Assert Failure - FIFO is empty and being read!
```

**预期结果：** 因为严重级别是 `warning`，测试**仍然通过**（VUnit 不会把 warning 计为失败）；告警只是打印在日志里。这正好印证 4.3.1 的结论——断言是「只提醒、不挡道」。对照未开启时的日志（无任何告警），体会 `UNDER_AND_OVERFLOW_ASSERTIONS` 开关的作用。

#### 4.3.5 小练习与答案

**练习 1：** 去掉 `-- synthesis off` / `-- synthesis on` 这对注释，保留 `generate` 开关，综合时还会不会有问题？

**参考答案：** `generate` 受 generic 控制，若 generic 在综合时为 `false`（默认值），这段代码不会生成任何硬件，综合无碍；但若综合时 generic 被设为 `true`，则 `report ... severity warning` 语句不可综合，综合工具会报错。这对注释是「即便有人误开了 generic，综合也不会崩」的第二道保险。

**练习 2：** 断言检查的是 `write_enable and full`，而不是 `fifo_write_request`。如果你改成检查 `fifo_write_request and full`，还能抓到「满写」吗？

**参考答案：** 抓不到了。因为 `fifo_write_request = write_enable and not full`，当 `full=1` 时 `fifo_write_request` 恒为 0，`fifo_write_request and full` 永远为假，断言永远不会触发。所以必须用**原始使能** `write_enable` 来表达「使用方试图违规」的意图。

---

## 5. 综合实践

把本讲三节串起来，完成一个小任务：**手工预测水位、再用波形验证、最后用断言兜底。**

**任务：** 针对 `FIFO_DEPTH = 8`（方便手算）的 `own_behavioural_sync_fifo`，构造如下激励，逐拍预测 `fifo_fill_level` / `full` / `empty` / `words_stored`，再跑仿真核对。

```text
拍号:    0   1   2   3   4   5   6   7   8   9   10  11  12 ...
wr_en:   1   1   1   1   1   1   1   1   1   0   1   1   ...
rd_en:   0   0   0   0   0   0   0   0   1   1   1   1   ...
水位:    0→1 1→2 2→3 3→4 4→5 5→6 6→7 7→8 8→8 8→7 7→7 7→7 ...   ← 请自行推算并核对
```

要点：

- 第 0–7 拍：只写，水位 0→8，第 7 拍后 `full` 应拉高。
- 第 8 拍：`wr_en=1, rd_en=1` 且 `full=1`。注意此时写请求被屏蔽（满），只读，水位 `8→7`，`full` 下一拍释放。这是一个容易推错的边界——确认你的预测与波形一致。
- 第 9 拍：只读，水位 `7→6`……（请补全）。
- 第 10 拍起：同时读写且不满不空，水位应**保持不变**（4.2 的核心结论）。

**操作：**

1. 在测试台里把 `FIFO_DEPTH` 改为 `8`（或在你的例化副本里设为 8），写入一段可识别的数据序列。
2. 用 [tb_fifo_sync.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.do) 的波形分组查看 `fifo_fill_level / write_pointer / read_pointer / fifo_*_request / full / empty`。
3. 把第 8 拍（满时同时读写）的水位变化与你手算的预测对比；若有出入，回到 [fifo_sync.vhd:183-214](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L183-L214) 复盘 `mem_req_proc` 的屏蔽逻辑。
4. 开启 `UNDER_AND_OVERFLOW_ASSERTIONS => true`，在第 0–7 拍之外的某拍故意制造一次「满写」或「空读」，确认日志里出现 `warning` 告警而测试不挂。

> 若本地暂无仿真器，第 1–3 步可降级为「源码阅读型实践」：仅依据本讲给出的公式与 [fifo_sync.vhd:189-214](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L189-L214) 手算水位序列，并说明每个边界的依据。

## 6. 本讲小结

- `own_behavioural_sync_fifo` 是厂商无关、可开箱仿真的同步 FIFO 实现，通过把 `dual_clock_dual_port_ram` 的两个时钟端口都接 `sys_clk` 来复用那块「哑存储」作为存储底座。
- 同步 FIFO 的满空**完全由一个填充水位计数器 `fifo_fill_level`** 决定（`full`：水位 ≥ 深度；`empty`：水位 = 0），无需指针比较——这是它区别于异步 FIFO（u9-l3）的根本所在。
- 写/读请求会先被满空**屏蔽**成 `fifo_write_request` / `fifo_read_request`，因此「满写 / 空读」会被安全忽略，不损坏数据。
- **同时读写时水位保持不变**（两指针都 +1），这是 FIFO 在稳态下维持每拍 1 字吞吐的关键；该分支在 `fifo_ctrl_proc` 中被刻意放在最前。
- `words_stored` 在 `full` 时钳位到 `FIFO_DEPTH`，是对外输出的防御性处理。
- `UNDER_AND_OVERFLOW_ASSERTIONS` 控制的断言块被 `-- synthesis off` 包裹、默认关闭、严重级别为 `warning`，是「只提醒、不挡道」的仿真专用自检。

## 7. 下一步学习建议

- **u9-l2 厂商 FIFO 封装：** 看同样的 entity 如何封装 Xilinx `xpm_fifo_sync` 与 Intel `scfifo`，体会「同一端口契约、三套架构」在存储 IP 上的完整落地，并理解大量厂商输出信号为何被接到 `*_unconnected`。
- **u9-l3 异步 FIFO 与格雷码指针：** 当读写时钟不同时，水位计数器不再可用，必须改用「格雷码指针跨域 + 指针比较」判满空——那是本讲「水位法」的对照面，理解本讲后会更清楚两种方法各自的适用边界。
- **u9-l4 异步 FIFO 的满空标志与读指针重放：** 进一步看异步 FIFO 如何用指针 MSB 区分满空、以及 `reset_read_pointer` 这一同步 FIFO 没有的「数据重放」机制。
- 建议继续精读 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) 的另外两套架构，并把本讲的 `tb_fifo_sync` 作为「同一测试台回归两套实现」的范本，在学完 u11 后能更深入地理解其验证方法学。
