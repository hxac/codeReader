# RAM 实现：单端口 / 简单双端口 / 真双端口

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 FPGA 中 RAM 的三种常见拓扑——单端口（SP）、简单双端口（SDP）、真双端口（TDP）——各自的端口结构与适用场景。
- 读懂 Open Logic 中 `olo_base_ram_sp` / `olo_base_ram_sdp` / `olo_base_ram_tdp` 三个实体的接口与内部「公共/私有」拆分写法。
- 理解同一地址同时读写时存在的「读前写 RBW / 写前读 WBR」歧义，以及为什么必须用 `RamBehavior_g` 提供两种行为。
- 通过仿真亲眼看到 RBW 与 WBR 在同地址读写时的输出差异，并能根据目标器件选择正确的取值。

## 2. 前置知识

在进入源码前，先建立几个直觉。

**FPGA 里的存储资源**。FPGA 内部存数据主要有三类资源：触发器（Flip-Flop，FF）、分布式 RAM（distributed RAM，由查找表 LUT 拼成）和块 RAM（block RAM，BRAM，芯片里专门的存储硬块）。触发器最灵活但最贵（每个只存 1 bit），块 RAM 最密集最便宜（一块可存数万 bit）。因此，**只要描述方式「长得像」一块 RAM，综合工具就会尽量把它推断（infer）成块 RAM**，而不是展开成一堆触发器。Open Logic 的三个 RAM 实体就是用纯 VHDL 写出「能被推断成块 RAM」的描述——这正是 u1-l1 讲过的 *Pure VHDL* 哲学：不调用厂商原语，但写法上配合各家工具的推断规则。

**同步 RAM 的基本时序**。所谓「同步 RAM」，是指地址、数据都在时钟上升沿被采样，读出的数据在若干拍后才出现在输出端口上。最常见的读延迟（read latency）是 1 拍：第 N 拍给出地址，第 N+1 拍输出对应内容。

**地址宽度**。一个深度为 `Depth_g` 的 RAM，其地址宽度需要满足：

\[
\text{AddrWidth} = \lceil \log_2(\text{Depth\_g}) \rceil
\]

这正是 Open Logic 在 u2-l1 讲过的 `log2ceil` 函数，所以你会看到端口写成 `std_logic_vector(log2ceil(Depth_g)-1 downto 0)`。

**读时写歧义（read-during-write）**。这是本讲的核心难点。设想第 N 拍对地址 A **同时**发起一次读和一次写：读想要地址 A 的内容，写又把新值塞进地址 A。那么第 N+1 拍的读端口到底输出**旧值**还是**新值**？两种答案都合理，对应两种行为：

- **RBW（Read-Before-Write，读前写）**：先读出旧值，再写入新值 → 读端口返回**旧值**。
- **WBR（Write-Before-Read，写前读）**：先写入新值，再读 → 读端口返回**新值**。

关键点在于：**不同 FPGA 的块 RAM 硬件，其原生行为并不统一**。有的器件天然是 RBW，有的天然是 WBR。如果你的 RTL 描述和器件原生行为不一致，综合工具就无法把它映射到块 RAM，只能退化成触发器或分布式 RAM，面积与时序都会变差。因此 Open Logic 给出 `RamBehavior_g` 这个开关，让你「按器件对号入座」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| [src/base/vhdl/olo_base_ram_sp.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd) | 单端口 RAM：读/写共用同一组地址与数据线。文件内还含私有实体 `olo_private_ram_sp_nobe`。 |
| [src/base/vhdl/olo_base_ram_sdp.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd) | 简单双端口 RAM：独立的写端口与读端口，可选双时钟。文件内含私有实体 `olo_private_ram_sdp_nobe`。 |
| [src/base/vhdl/olo_base_ram_tdp.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_tdp.vhd) | 真双端口 RAM：A、B 两个端口都能读能写。文件内含私有实体 `olo_private_ram_tdp_nobe`。 |
| [doc/base/olo_base_ram_sdp.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_ram_sdp.md) | 简单双端口 RAM 的官方文档，含各 generic 取值与厂商 `RamStyle_g` 速查。 |
| [test/base/olo_base_ram_sdp/olo_base_ram_sdp_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_ram_sdp/olo_base_ram_sdp_tb.vhd) | 简单双端口 RAM 的 VUnit 测试台，其中的 `ReadDuringwrite` 用例正是 RBW/WBR 行为对比。 |
| [sim/test_configs/olo_base.py](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py) | 把三个 RAM 测试台按 `RamBehavior_g` 等泛型组合注册成多组测试用例。 |

> 命名提示：u1-l2 讲过实体命名 `olo_<area>_<function>`。本讲的私有实体带 `olo_private_` 前缀且以 `_nobe`（no byte-enable）结尾，表示「不带字节使能的内部实现」，仅供库内部实例化使用。

## 4. 核心概念与源码讲解

### 4.1 三种 RAM 拓扑的端口差异

#### 4.1.1 概念说明

三种拓扑的差别，可以用「同一块存储阵列上能同时开几扇门、每扇门能读还是写」来理解：

| 拓扑 | 端口数 | 读/写关系 | 典型用途 |
| :--- | :--- | :--- | :--- |
| **单端口（SP）** | 1 个 | 同一端口分时读/写，地址共用 | 一块只能被一个主人访问的查找表/缓存 |
| **简单双端口（SDP）** | 2 个 | 一个只写、一个只读 | 一边持续写入、另一边持续读出的流式缓冲（FIFO 底层常用） |
| **真双端口（TDP）** | 2 个 | 每个端口都能读能写 | 两个时钟域/模块共享一块内存、双口寄存器组 |

端口越多，硬件资源越贵——并不是所有器件的块 RAM 都支持真双端口，也不一定都支持双时钟。所以「按需选最便宜的拓扑」是一条基本原则。

#### 4.1.2 三者接口对比

**单端口 RAM** 只有一组地址与数据，读、写共用。其端口（节选）如下：

[olo_base_ram_sp.vhd:45-55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd#L45-L55) —— 单端口的 `Clk / Addr / WrEna / WrData / RdEna / RdData / RdValid`，读写共用 `Addr`：

```vhdl
port (
    Clk      : in    std_logic;
    Rst      : in    std_logic                                  := '0';
    Addr     : in    std_logic_vector(log2ceil(Depth_g)-1 downto 0);
    Be       : in    std_logic_vector(Width_g / 8 - 1 downto 0) := (others => '1');
    WrEna    : in    std_logic                                  := '1';
    WrData   : in    std_logic_vector(Width_g - 1 downto 0);
    RdEna    : in    std_logic                                  := '1';
    RdData   : out   std_logic_vector(Width_g - 1 downto 0);
    RdValid  : out   std_logic
);
```

**简单双端口 RAM** 把读、写拆成两套独立信号，写侧用 `Wr_*`、读侧用 `Rd_*`，并且可选独立读时钟 `Rd_Clk`：

[olo_base_ram_sdp.vhd:46-59](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd#L46-L59) —— `Wr_Addr/Wr_Ena/Wr_Data` 与 `Rd_Addr/Rd_Ena/Rd_Data` 分离，`IsAsync_g=true` 时读侧改用 `Rd_Clk`。

注意地址宽度仍按 `log2ceil(Depth_g)-1 downto 0` 写法，与 SP 完全一致。

**真双端口 RAM** 直接给出 A、B 两套对称端口，每套都含自己的时钟、地址、写使能、写数据与读数据：

[olo_base_ram_tdp.vhd:46-65](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_tdp.vhd#L46-L65) —— A 口与 B 口结构镜像，每口可独立读写，默认 `A_WrEna`/`B_WrEna` 为 `'0'`（默认读）。

#### 4.1.3 三者共享的泛型

三个实体共享几乎相同的泛型集合，只是 SDP 多一个 `IsAsync_g`。以 SP 为例：

[olo_base_ram_sp.vhd:35-44](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd#L35-L44) 中可见 `Depth_g / Width_g / RdLatency_g / RamStyle_g / RamBehavior_g / UseByteEnable_g / InitString_g / InitFormat_g`。

要点：

- `Depth_g` / `Width_g`：地址数与每地址的位宽。
- `RdLatency_g`：读延迟拍数，默认 1；可加大以换时序余量。
- `RamStyle_g`：传给厂商综合属性（如 AMD 的 `"block"`/`"distributed"`），控制映射到哪种存储资源。
- `RamBehavior_g`：RBW/WBR 行为开关，本讲核心。
- `InitString_g` / `InitFormat_g`：内存初值（`"HEX"` + 逗号分隔、每个值带 `0x` 前缀）。

#### 4.1.4 小练习与答案

**练习**：SDP 比 SP 多出哪些端口？比 TDP 又少了什么？

**参考答案**：SDP 比 SP 多出一个完整的读端口（`Rd_Clk / Rd_Addr / Rd_Ena / Rd_Data / Rd_Valid`），使读写可以同时发生；但相比 TDP，SDP 的两个端口是「专职」的（一写一读），而 TDP 的 A/B 两口各自都既能读又能写。

---

### 4.2 公共实体与私有实体的拆分（olo_base_ram_* 与 olo_private_ram_*_nobe）

#### 4.2.1 概念说明

打开任一 RAM 源文件你会发现：**一个文件里有两个实体**。外层 `olo_base_ram_sp` / `olo_base_ram_sdp` / `olo_base_ram_tdp` 是用户直接实例化的「公共实体」；内层 `olo_private_ram_sp_nobe` / `olo_private_ram_sdp_nobe` / `olo_private_ram_tdp_nobe` 是「私有实体」，`_nobe` 表示 *no byte-enable*——只实现不带字节使能的纯存储阵列。

为什么这样拆？因为字节使能（byte enable）是一个「正交」的功能：

- 没有字节使能时，一块 RAM 就是一个完整的存储阵列。
- 启用字节使能后，本质上等价于「把一块宽 RAM 拆成若干个 8-bit 的窄 RAM，每个窄 RAM 各带一个写使能」。

公共实体负责这两件事：(1) 按字节把宽 RAM 拆成一堆 8-bit 私有 RAM；(2) 维护 `RdValid` 流水线和各类断言。私有实体只关心最纯粹的「存储阵列 + 读写时序」。这样字节使能的逻辑集中写一次，三种拓扑都能复用同一套写法。

#### 4.2.2 核心流程：字节拆分与零拍直通

公共实体用 `for generate` 把位宽按字节拆开。SP 的写法（SDP/TDP 同理）：

[olo_base_ram_sp.vhd:129-159](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd#L129-L159) —— 启用字节使能时，按 `BeCount_c = Width_g/8` 循环，每个 8-bit 切片各实例化一个 `olo_private_ram_sp_nobe`，写使能由 `WrEna and Be(byte)` 决定。

```vhdl
g_be : if UseByteEnable_g generate
    g_byte : for byte in 0 to BeCount_c-1 generate
        signal WrEna_Byte : std_logic;
    begin
        WrEna_Byte <= WrEna and Be(byte);
        i_ram : component olo_private_ram_sp_nobe
            generic map ( Depth_g => Depth_g, Width_g => 8, ... )
            port map ( Clk => Clk, Addr => Addr,
                       WrEna => WrEna_Byte,
                       WrData => WrData(byte*8+7 downto byte*8), ... );
    end generate;
end generate;
```

未启用字节使能时则直接实例化一个满宽的私有 RAM：

[olo_base_ram_sp.vhd:104-126](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd#L104-L126) —— `g_nobe` 分支把整个 `Width_g` 原样交给一个私有 RAM。

字节使能会带来面积代价，官方文档明确建议「非必要不开启」，原因就在这里：它会把一块大 RAM 拆成多个小 RAM，破坏合并推断。

#### 4.2.3 存储阵列与初值解析

存储阵列本身定义在私有实体里，是一个数组类型的 `shared variable`：

[olo_base_ram_sp.vhd:224-266](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd#L224-L266) —— `type Data_t is array (...) of std_logic_vector(...)`，`shared variable Mem_v : Data_t(...) := getInitContent;`，并用 `ram_style`/`ramstyle`/`syn_ramstyle` 三个跨厂商属性把 `RamStyle_g` 同时下发给各家工具。

> 用 `shared variable` 而非 `signal` 是为了让读、写能在同一个时钟进程里对同一存储对象赋值（VHDL 对 `signal` 的多驱动有限制）。`getInitContent` 函数解析 `InitString_g` 里的逗号分隔十六进制（注释说明它不能放进 package，因为要兼容 VHDL-93 对非约束数组返回值的限制）。

#### 4.2.4 小练习与答案

**练习**：为什么字节使能会「增加资源占用」？

**参考答案**：启用字节使能后，公共实体把一块 `Width_g` 位宽的 RAM 拆成 `Width_g/8` 块 8-bit RAM，每块都需要独立的地址译码与写使能逻辑，综合工具往往无法把它们重新合并回单个块 RAM，因此占用更多资源。官方因此建议除非确有必要，否则关闭 `UseByteEnable_g`。

---

### 4.3 读写时序与 RBW/WBR 行为切换

#### 4.3.1 概念说明

私有实体里的核心是一个时钟进程，它同时做「读」和「写」两件事。三者的写法在 SP/SDP（同步模式）里几乎一样——区别只在于**读和写在进程里的先后顺序**。这正是 RBW/WBR 的实现原理：

- 先执行读、再执行写 → 读到的是写之前的旧值 → **RBW**。
- 先执行写、再执行读 → 读到的是刚写入的新值 → **WBR**。

注意：这里「先/后」指的是**同一时钟沿内代码的书写顺序**。因为 `Mem_v` 是 `shared variable`（变量赋值立即生效，不像信号要到下一拍），所以同一进程里后一条语句能看到前一条语句刚写入的值——这正是能区分两种行为的关键。

TDP 的写法稍有不同：它用两个 `if generate`（`g_wbr` 与 `g_rbw`）分别给出两套端口进程，而 SP/SDP 用同一个进程内 `if` 分支调换顺序。

#### 4.3.2 核心流程：同一进程内的顺序决定行为

来看 SP 私有实体的 RAM 进程：

[olo_base_ram_sp.vhd:290-310](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd#L290-L310) —— 同一个 `rising_edge(Clk)` 内，RBW 分支「先读后写」，WBR 分支「先写后读」。

```vhdl
p_ram : process (Clk) is
begin
    if rising_edge(Clk) then
        if compareNoCase(RamBehavior_g, "RBW") then
            if RdEna = '1' then
                RdPipe(1) <= Mem_v(to_integer(unsigned(Addr)));   -- 先读（旧值）
            end if;
        end if;
        if WrEna = '1' then
            Mem_v(to_integer(unsigned(Addr))) := WrData;          -- 再写
        end if;
        if not compareNoCase(RamBehavior_g, "RBW") then           -- 即 WBR
            if RdEna = '1' then
                RdPipe(1) <= Mem_v(to_integer(unsigned(Addr)));   -- 后读（新值）
            end if;
        end if;
        RdPipe(2 to RdLatency_g) <= RdPipe(1 to RdLatency_g-1);   -- 输出流水线
    end if;
end process;
```

SDP 同步模式的 RAM 进程与之几乎逐字相同，只是把 `Addr` 换成读写各自的 `Wr_Addr` / `Rd_Addr`：

[olo_base_ram_sdp.vhd:330-350](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd#L330-L350) —— 同样的「读在前=RBW / 读在后=WBR」结构。

TDP 则把两种行为写成两套互斥的 `generate`：

[olo_base_ram_tdp.vhd:337-369](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_tdp.vhd#L337-L369) —— `g_wbr` 分支：先写 `Mem_v(...) := A_WrData`，再 `RdPipeA(1) <= Mem_v(...)`（读到新值）。

[olo_base_ram_tdp.vhd:371-403](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_tdp.vhd#L371-L403) —— `g_rbw` 分支：先读 `RdPipeA(1) <= Mem_v(...)`（读到旧值），再写。

每个私有实体开头还有一条断言，确保 `RamBehavior_g` 只能是这两种之一：

[olo_base_ram_sdp.vhd:323-325](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd#L323-L325) —— 若 `RamBehavior_g` 既非 `"RBW"` 也非 `"WBR"`，仿真时立即报 error。

#### 4.3.3 原理补充：为什么要两种行为

理想世界里，我们希望 RTL 描述永远只有一种确定行为。但现实中：

1. 不同厂商、不同系列的块 RAM 硬件，原生「同地址同周期读写」行为不统一。
2. 综合工具只有当你的 RTL 行为与目标块 RAM 原生行为一致时，才能把它映射成块 RAM；不一致时只能退化成触发器/分布式 RAM，面积与时序都变差。
3. 有的器件甚至只有某一种行为能映射到分布式 RAM。

因此 Open Logic 不替你做决定，而是把 `RamBehavior_g` 暴露出来，并在文档中建议：**不确定时两种都试一次，看综合报告里哪一种被正确映射成了块 RAM**。

#### 4.3.4 小练习与答案

**练习**：如果把 `shared variable Mem_v` 改成 `signal Mem_v`，RBW 与 WBR 还能区分吗？

**参考答案**：不能（至少行为会改变）。`signal` 的赋值要到下一个仿真周期才生效，同一进程内后一条语句读到的仍是旧值，导致 WBR 分支也读到旧值、与 RBW 无法区分。这正是这里必须用 `shared variable`（变量赋值立即生效）的原因。

---

### 4.4 读延迟流水线、RdValid 与综合属性

#### 4.4.1 概念说明

除了存储阵列，每个 RAM 还维护两条「副」流水线：

- **读数据流水线 `RdPipe`**：把第 1 拍的读结果再寄存 `RdLatency_g-1` 拍，从而支持大于 1 的读延迟，用于高速设计里换时序余量。
- **读有效流水线 `RdValidPipe`**：把 `Rd_Ena` 同样打 `RdLatency_g` 拍，产生 `RdValid`，告诉下游「此刻 `RdData` 有效」。这让下游逻辑不必关心具体读延迟是多少拍。

此外，Open Logic 故意给这两条流水线加上 `shreg_extract = suppress` 综合属性，**阻止综合工具把它们吸收进移位寄存器（SRL）**——因为一旦被吸收成 SRL，存储阵列与输出寄存器就不再「连续」，可能反过来影响块 RAM 的推断。

#### 4.4.2 核心流程：RdValid 流水线

SDP 的 `RdValid` 流水线分同步/异步两套（由 `IsAsync_g` 选择），同步版用 `Clk`、异步版用 `Rd_Clk`：

[olo_base_ram_sdp.vhd:174-210](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd#L174-L210) —— `g_sync_valid` 与 `g_async_valid` 两个 `generate` 分别在 `Clk` / `Rd_Clk` 上把 `Rd_Ena` 打拍得到 `Rd_Valid`，复位只清零这条流水线。

```vhdl
g_sync_valid : if not IsAsync_g generate
    p_rdvalid : process (Clk) is
    begin
        if rising_edge(Clk) then
            RdValidPipe(1)                <= Rd_Ena;
            RdValidPipe(2 to RdLatency_g) <= RdValidPipe(1 to RdLatency_g-1);
            if Rst = '1' then
                RdValidPipe <= (others => '0');   -- 仅复位 RdValid，不动存储内容
            end if;
        end if;
    end process;
end generate;
```

TDP 因为有两个端口，所以有 A、B 两条 `RdValid` 流水线，分别跑在 `A_Clk` 和 `B_Clk` 上：

[olo_base_ram_tdp.vhd:187-216](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_tdp.vhd#L187-L216) —— `p_rdvalid_a`（`A_Clk`）与 `p_rdvalid_b`（`B_Clk`）对称。

抑制移位寄存器抽取的属性声明，以 SP 为例：

[olo_base_ram_sp.vhd:70-72](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sp.vhd#L70-L72) —— `attribute shreg_extract of RdValidPipe : signal is ShregExtract_SuppressExtraction_c;`，该跨厂商常量来自 u2-l1 讲过的 `olo_base_pkg_attribute` 包。

> 一条贯穿三处的重要事实：**复位只作用于 `RdValid` 流水线，不复位存储内容**。文档里反复强调 *Does NOT reset the content of memory cells!*——上电后 RAM 内容是未定义的（除非用 `InitString_g` 指定）。

#### 4.4.3 小练习与答案

**练习**：为什么要在 `RdValidPipe` 上加 `shreg_extract = suppress`？

**参考答案**：综合工具可能把一串触发器优化成移位寄存器原语（如 AMD 的 SRL）。一旦 `RdValid` 流水线（以及读数据流水线 `RdPipe`）被抽成 SRL，它们与存储阵列之间的寄存器结构就被打破，可能妨碍工具把整块推断成带输出寄存器的块 RAM。抑制抽取可让这些寄存器以独立 FF 形式保留，保证块 RAM 推断稳定。

---

### 4.5 简单双端口 RAM 的异步模式（IsAsync_g）

#### 4.5.1 概念说明

SDP 的 `IsAsync_g=true` 是三拓扑中唯一支持「双时钟」的模式：写端口跑在 `Clk`、读端口跑在独立的 `Rd_Clk`。注意它和 u3-l1 将讲的异步 FIFO 不同——这里**不保证跨时钟域的数据安全性**，它只是提供两个独立的时钟沿，由使用者保证两侧不会在同一时刻对同一存储单元产生竞争。它常用于读写时钟严格配合、或一方只配置只读的场景。

#### 4.5.2 核心流程：写读进程分离

异步模式下，写、读拆成两个进程，分别由各自的时钟驱动：

[olo_base_ram_sdp.vhd:354-380](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd#L354-L380) —— `p_write` 在 `Clk` 上升沿写 `Mem_v`；`p_read` 在 `Rd_Clk` 上升沿把 `Mem_v(Rd_Addr)` 读进 `RdPipe`。注意异步分支不再区分 RBW/WBR，因为读写已分属不同时钟域，同沿竞争的概念不适用。

公共实体里的 `RdValid` 流水线也相应地在 `Rd_Clk` 上跑：

[olo_base_ram_sdp.vhd:193-208](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd#L193-L208) —— `g_async_valid` 分支用 `Rd_Clk` 打拍，并用 `Rd_Rst` 复位。

#### 4.5.3 代码实践：观察 IsAsync_g 的时钟分离

**实践目标**：从仿真层面确认异步模式下写、读分属不同时钟域。

**操作步骤**：阅读 SDP 测试台里 `IsAsync_g` 为真时的时钟生成与复位时序。

[olo_base_ram_sdp_tb.vhd:142-144](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_ram_sdp/olo_base_ram_sdp_tb.vhd#L142-L144) —— 当 `IsAsync_g` 为真时，`Rd_Clk` 以 33.3 ns 周期独立翻转（`Clk` 周期为 10 ns），二者频率不同。

**需要观察的现象**：写侧（`Clk`，100 MHz）与读侧（`Rd_Clk`，约 30 MHz）频率不同；复位分别用 `Rd_Rst`（读侧）和 `Rst`（同步侧）。

**预期结果**：异步用例下，`Rd_Valid` 与 `Rd_Data` 的变化节拍跟随 `Rd_Clk`，而非 `Clk`。

---

## 5. 综合实践：用 SDP 对比 RBW 与 WBR 的同地址读写输出

本实践把第 4.3 节的行为差异用仿真「看」出来。仓库已经替我们准备好了用例与泛型组合，我们只需运行并解读结果。

### 5.1 实践目标

实例化 `olo_base_ram_sdp`（仓库的测试台已经替我们实例化好），在 `RamBehavior_g="RBW"` 与 `"WBR"` 两种配置下，对**同一地址同时读写**，对比读端口输出，验证 RBW 返回旧值、WBR 返回新值。

### 5.2 背景：测试台已经实现了这个场景

测试台里有一个名为 `ReadDuringwrite` 的用例，正是为对比 RBW/WBR 而写。它的核心逻辑是：先往地址 1/2/3 写入 5/6/7，然后**对同一地址同时发起写和读**（写入新值 1/2/3），并根据 `RamBehavior_g` 期望不同的读回值：

[olo_base_ram_sdp_tb.vhd:317-341](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_ram_sdp/olo_base_ram_sdp_tb.vhd#L317-L341) —— 初始化后，同周期 `Wr_Addr=1, Rd_Addr=1, Wr_Data=1`；读延迟为 1 时，RBW 期望 `Rd_Data=5`（旧值），WBR 期望 `Rd_Data=1`（新值）。

```vhdl
if RdLatency_g = 1 then
    if RamBehavior_g = "RBW" then
        check_equal(Rd_Data, 5, "rw: 1=5");     -- 读到旧值
    else
        check_equal(Rd_Data, 1, "rw: 1=1 wbr"); -- 读到新值
    end if;
end if;
```

### 5.3 背景：泛型组合已在 test_configs 里注册

`sim/test_configs/olo_base.py` 把 SDP 测试台针对 `RamBehavior_g` 取 `RBW`/`WBR` 各注册了一组具名配置：

[sim/test_configs/olo_base.py:53-60](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L53-L60) —— 三个 RAM 测试台都按 `RamBehavior_g` 各跑两遍，SDP 还额外叠加 `IsAsync_g`。

```python
ram_tbs = ['olo_base_ram_sp_tb', 'olo_base_ram_tdp_tb', 'olo_base_ram_sdp_tb']
for tb_name in ram_tbs:
    tb = olo_tb.test_bench(tb_name)
    for RamBehav in ['RBW', 'WBR']:
        named_config(tb, {'RamBehavior_g': RamBehav})
```

所以你无需自己写泛型组合，直接运行即可同时覆盖两种行为。

### 5.4 操作步骤

1. 进入仿真目录：

   ```bash
   cd sim
   ```

2. 用 GHDL（默认）只跑 SDP 的 `ReadDuringwrite` 用例（VUnit 支持用通配符筛选用例名）：

   ```bash
   python3 run.py "*olo_base_ram_sdp_tb*ReadDuringwrite*"
   ```

   > 该命令的具体通配形式请**待本地验证**：VUnit 默认对「库.测试台.配置.用例」做 glob 匹配，`*olo_base_ram_sdp_tb*ReadDuringwrite*` 通常能命中 RBW 与 WBR 两组配置下的同名用例。若未命中，可去掉 `ReadDuringwrite` 直接跑全部 SDP 用例：`python3 run.py "*olo_base_ram_sdp_tb*"`。

3. 若想换仿真器（按 u1-l4 讲过的方式）：

   ```bash
   python3 run.py --nvc  "*olo_base_ram_sdp_tb*ReadDuringwrite*"
   ```

### 5.5 需要观察的现象与预期结果

- 两种 `RamBehavior_g` 配置都会被执行（VUnit 会列出形如 `...RamBehavior_g-RBW.ReadDuringwrite` 与 `...RamBehavior_g-WBR.ReadDuringwrite` 两个用例）。
- 两者的初始化完全相同（地址 1/2/3 先写 5/6/7），随后对同地址写新值 1/2/3 并同时读。

预期（读延迟为 1 时）：

| 周期（写到同地址 X 同时读 X） | RBW 读出 | WBR 读出 |
| :--- | :--- | :--- |
| X=1 | 5（旧值） | 1（新值） |
| X=2 | 6（旧值） | 2（新值） |
| X=3 | 7（旧值） | 3（新值） |

随后测试台再做一次普通读回，两者都会得到新写入的 1/2/3（见 [olo_base_ram_sdp_tb.vhd:381-383](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_ram_sdp/olo_base_ram_sdp_tb.vhd#L381-L383)）——说明 RBW/WBR 的差异只出现在「同地址同周期读写」那一拍，写操作本身在两种模式下都成功落盘。

**两组用例都应 `PASS`**，这正是「两种行为都自洽且都被设计支持」的证据。

### 5.6 进阶（可选）

- 把筛选条件换成 `*olo_base_ram_sp_tb*` 与 `*olo_base_ram_tdp_tb*`，确认 SP、TDP 在 RBW/WBR 下也各自通过——三者用的是同一套行为定义。
- 在自己选定的厂商工具里，分别用 `RamBehavior_g="RBW"` 与 `"WBR"` 综合 SDP，对照综合报告里的「RAM 资源使用」一栏，看哪一种被映射成了块 RAM。这与本讲反复强调的选型方法一致。

## 6. 本讲小结

- Open Logic 用三个实体覆盖 RAM 的三种拓扑：单端口 `olo_base_ram_sp`、简单双端口 `olo_base_ram_sdp`、真双端口 `olo_base_ram_tdp`，端口越多资源越贵，按需选最便宜的。
- 每个实体都采用「公共实体 + 私有实体（`_nobe`）」拆分：公共实体负责字节使能拆分、`RdValid` 流水线与断言，私有实体持有真正的存储阵列。
- 存储用 `shared variable` 实现，使同一进程里读、写的先后顺序能直接体现 RBW（读前写，返回旧值）与 WBR（写前读，返回新值）两种行为。
- `RamBehavior_g` 是「按器件对号入座」的开关：RTL 行为必须与目标块 RAM 原生行为一致，才能被正确推断成块 RAM；不确定时两种都试，看综合报告。
- 读延迟通过 `RdPipe`（数据）和 `RdValidPipe`（有效）两条流水线实现，并用 `shreg_extract=suppress` 阻止它们被抽成移位寄存器，以稳定块 RAM 推断。
- 复位**只清 `RdValid`，不清存储内容**；初值只能靠 `InitString_g`/`InitFormat_g` 指定。

## 7. 下一步学习建议

- **接下来读 u2-l4（同步 FIFO）**：同步 FIFO 正是建立在 SDP RAM 之上的，你可以看到本讲的 RAM 如何被组装成带读写指针、几乎满/几乎空状态的完整 FIFO。
- **后续读 u3-l1（异步 FIFO）**：异步 FIFO 用双时钟与格雷码指针安全地跨时钟域，与 SDP 的 `IsAsync_g` 异步模式做对比，理解「安全的跨时钟域存储」与「单纯的异步双口 RAM」的差别。
- **想深挖综合推断**：阅读 [doc/base/olo_base_ram_sdp.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_ram_sdp.md) 中各厂商 `RamStyle_g` 取值表，并在厂商工具里实测 RBW/WBR 对块 RAM 映射的影响。
