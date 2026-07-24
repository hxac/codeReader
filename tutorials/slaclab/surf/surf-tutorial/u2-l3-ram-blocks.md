# RAM 构建块（base/ram）

## 1. 本讲目标

FPGA 里几乎所有「带状态的批量数据」——FIFO 的存储体、寄存器堆、DMA 的缓冲、查找表、帧缓存——最终都要落到两类物理资源上：**块 RAM（Block RAM, BRAM）** 和 **分布式 RAM（distributed RAM，由 LUT 拼成）**。SURF 把这两类资源的访问封装成了一组统一、可参数化的 VHDL 构建块，放在 `base/ram/`。

学完本讲，你应当能够：

1. 看懂 `SimpleDualPortRam`（一写一读的简单双口）、`TrueDualPortRam`（两口都可读写的真双口）、`LutRam`（分布式、多读口）三者的端口、写时序与读时序差异。
2. 理解「字节写使能（byte write enable）」如何把一个宽字拆成若干字节通道，并对应到 BRAM 原生的字节写能力。
3. 说清 VHDL 里 `shared variable mem` + `attribute ram_style` 是如何「提示」综合器把一段数组推断成 block RAM 或 distributed RAM 的。
4. 在 `inferred`（可推断）、`xilinx/`（XPM 宏封装）、`SinglePortRamPrimitive`（直接例化原语）、`dummy`（占位）这几种后端之间做出正确选择，并理解 `ruckus.tcl` 为何按 Vivado 版本对它们做门控。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。本讲假定你已经读过 [u1-l4 StdRtlPkg 约定](u1-l4-stdrtlpkg-conventions.md)（`sl`/`slv`、`ite`、`_G`/`_C` 后缀、`TPD_G`/`RST_POLARITY_G`/`RST_ASYNC_G`）。

**块 RAM vs 分布式 RAM。** 这是本讲最核心的一对概念。

- **块 RAM（BRAM）** 是 FPGA 芯片里专门铺设的存储列（如 Xilinx 的 36Kb 块），容量大、密度高，但**读是同步的**——给地址后要等一个时钟沿才能拿到数据（读延迟 ≥ 1 拍）。它天然是真双口的：一个块有两个独立端口，各自有自己的地址/数据/时钟。
- **分布式 RAM（distributed / LUT RAM）** 用普通查找表（LUT）的 RAM 模式拼成，散布在逻辑阵列里。它**写是同步的，读是异步的（组合的）**——给地址当拍就能出数据（读延迟可为 0），而且可以有很多个读口；但单块容量小。

一句话决策：**要大容量、读能等一拍 → BRAM；要小容量、要异步读或要多读口 → distributed。**

**「推断（inference）」是什么意思。** VHDL 本身没有「声明一块 BRAM」的语法。我们写的是一个数组类型 `type MemType is array(...) of slv(...)`，再用一个时钟进程对它做同步写、对端口做读。综合器（Vivado/Quartus）看到这种**写法模板**，会自动识别（infer）出「哦，这是一块 RAM」，并映射到芯片里的 BRAM 或 LUT RAM 资源。为了让综合器「听话」，SURF 在数组对象上挂了一组属性（attribute），如 `ram_style => "block"` 表示「请用 BRAM」，`"distributed"` 表示「请用 LUT RAM」。

**双口 RAM 的写-读冲突模式（MODE）。** 当某个端口的同一地址「同一个时钟沿既写又读」时，输出端拿到的是旧数据还是新数据？这有三种约定（Xilinx 标准）：

| `MODE_G` | 同址同沿写读时，该端口输出 |
|---|---|
| `no-change` | 输出保持不变（写周期不刷新输出） |
| `read-first` | 输出旧数据（先读后写） |
| `write-first` | 输出新数据（先写后读） |

`TrueDualPortRam` 用三个 `generate` 分支分别实现了这三种模式。

## 3. 本讲源码地图

本讲涉及 `base/ram/` 下的文件，按「推断后端 → 显式后端 → 占位」分层：

| 文件 | 角色 |
|---|---|
| [base/ram/inferred/SimpleDualPortRam.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd) | **简单双口**：A 口只写、B 口只读；可推断为 BRAM 或 distributed；支持字节写。本讲主角。 |
| [base/ram/inferred/TrueDualPortRam.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/TrueDualPortRam.vhd) | **真双口**：A/B 两口都能独立读写；只推断为 BRAM；支持三种 `MODE_G`。 |
| [base/ram/inferred/DualPortRam.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/DualPortRam.vhd) | **路由封装**：按 `MEMORY_TYPE_G` 在 `TrueDualPortRam`（块）与 `LutRam`（分布式）间二选一。 |
| [base/ram/inferred/LutRam.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/LutRam.vhd) | **分布式 RAM**：1 个写口 + 最多 8 个异步读口；`ram_style="distributed"`。 |
| [base/ram/xilinx/SimpleDualPortRamXpm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SimpleDualPortRamXpm.vhd) | **XPM 封装**：直接例化 Xilinx `xpm_memory_sdpram` 宏，显式控制 BRAM 参数。 |
| [base/ram/xilinx/TrueDualPortRamXpm.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/TrueDualPortRamXpm.vhd) | **XPM 封装**：直接例化 `xpm_memory_tdpram`。 |
| [base/ram/xilinx/SinglePortRamPrimitive.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SinglePortRamPrimitive.vhd) | **原语封装**：按位例化 `RAM32X1S`…`RAM512X1S` 等 unisim 原语。 |
| [base/ram/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/ruckus.tcl) | **构建清单**：无条件加载 `inferred/`，按 Vivado 版本门控是否加载 `xilinx/`。 |

另外会引用到工具函数 `wordCount`（在 [base/general/rtl/StdRtlPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd) 中定义），以及一个真实使用方 [base/fifo/rtl/inferred/FifoSync.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd)——这正是 [u2-l2 FIFO](u2-l2-fifo-blocks.md) 里的同步 FIFO 存储体。

---

## 4. 核心概念与源码讲解

### 4.1 简单双口 RAM：SimpleDualPortRam

#### 4.1.1 概念说明

`SimpleDualPortRam` 是 SURF 里使用频率最高的 RAM 块——FIFO 的存储体几乎都是它。它是最朴素的「一写一读」双口：

- **Port A：只写。** 给 `addra` + `dina` + `wea`，在 `clka` 上升沿写入。
- **Port B：只读。** 给 `addrb`，在 `clkb` 上升沿读出 `doutb`（BRAM 的同步读）。

两个端口可以有**各自的时钟**（`clka`/`clkb`），这正是异步 FIFO 跨时钟域存储数据的那块内存。它既能被推断成 block RAM（默认），也能被推断成 distributed RAM，由 `MEMORY_TYPE_G` 决定。

#### 4.1.2 核心流程

写入侧（Port A）每拍的伪代码：

```
wait until rising_edge(clka);
if ena = '1':
    for 每个字节通道 i in 0..NUM_BYTES-1:
        if weaByteInt(i) = '1':           # 该字节写使能打开
            mem[addra] 的第 i 字节 := dina 的第 i 字节   # 只改这一个字节
```

读出侧（Port B）每拍的伪代码（含复位）：

```
if 异步复位生效:        doutBInt <= INIT_C        # 仅 RST_ASYNC_G=true
elsif rising_edge(clkb):
    if 同步复位生效:    doutBInt <= INIT_C        # 仅 RST_ASYNC_G=false
    elsif enb = '1':    doutBInt <= mem[addrb]    # 同步读
doutb <= doutBInt (可选再过一拍 DOB_REG_G)
```

「字节写」的关键在于：写使能不是单比特 `wea`，而是一个**每字节一位**的向量 `weaByte`。这样一次写可以只更新一个字里的某几个字节——这正是网络包处理、寄存器堆按字节改写所需要的，也恰好对应 BRAM 原生的字节写使能引脚。

#### 4.1.3 源码精读

先看实体与泛型，注意 `MEMORY_TYPE_G` 默认是 `"block"`，`BYTE_WR_EN_G` 默认关闭：

[SimpleDualPortRam.vhd:23-50](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd#L23-L50) — 实体声明：A 口（`clka/ena/wea/weaByte/addra/dina`）只写、B 口（`clkb/enb/regceb/rstb/addrb/doutb`）只读；`weaByte` 的宽度由 `wordCount(DATA_WIDTH_G, BYTE_WIDTH_G)` 算出（字节数）。

接着是几个派生常量，它们解决「数据位宽不是字节整数倍时如何整齐映射到 BRAM 字节通道」的问题：

[SimpleDualPortRam.vhd:54-66](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd#L54-L66) — 定义内部宽度：

- `BYTE_WIDTH_C`：若没开字节写（`BYTE_WR_EN_G=false`），把「字节宽」直接设成整个字宽 `DATA_WIDTH_G`——这样 BRAM 的奇偶位（parity，9 位字节里的第 9 位）也能被利用，不浪费。
- `NUM_BYTES_C := wordCount(DATA_WIDTH_G, BYTE_WIDTH_C)`：向上取整的字节数，即 `weaByte` 的位数。
- `FULL_DATA_WIDTH_C := NUM_BYTES_C * BYTE_WIDTH_C`：可能 ≥ `DATA_WIDTH_G`，是**存储阵列真正的字宽**（含可能的对齐填充）。
- `mem` 是 `shared variable`（共享变量），因为 BRAM 的写进程和读进程要共享同一段存储。

> 说明 `wordCount` 的语义——它就是「向上取整除法」，定义在 [StdRtlPkg.vhd:803-811](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L803-L811)：
> \[ \text{wordCount}(n, w) = \left\lceil \frac{n}{w} \right\rceil \]
> 所以 32 位数据、8 位字节 → 4 个字节；12 位数据、8 位字节 → 2 个字节（第二个字节只用 4 位有效位，靠下面的 `resize` 补齐）。

**最关键的一行**——决定推断成 block 还是 distributed 的属性：

[SimpleDualPortRam.vhd:72-77](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd#L72-L77) — 把泛型 `MEMORY_TYPE_G` 透传成综合属性：

```vhdl
constant XST_BRAM_STYLE_C : string := MEMORY_TYPE_G;
...
attribute ram_style        : string;
attribute ram_style of mem : variable is XST_BRAM_STYLE_C;
```

这里 `ram_style` 是 Xilinx 综合器认识的属性。`MEMORY_TYPE_G="block"` 时综合器优先用 BRAM；`="distributed"` 时优先用 LUT RAM。（紧随其后的 `syn_ramstyle`/`syn_keep` 是给 Synopsys/Synplicity 综合器的同名属性，作用一致——同一份代码兼容多家工具。）

写入进程——字节级写循环：

[SimpleDualPortRam.vhd:88-103](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd#L88-L103) — 写逻辑：

```vhdl
weaByteInt <= weaByte when BYTE_WR_EN_G else (others => wea);  -- 不开字节写时把 wea 广播到所有字节位
process(clka) ...
   for i in NUM_BYTES_C-1 downto 0 loop
      if (weaByteInt(i) = '1') then
         mem(conv_integer(addra))((i+1)*BYTE_WIDTH_C-1 downto i*BYTE_WIDTH_C) :=
            resize(dina(minimum(DATA_WIDTH_G-1, (i+1)*BYTE_WIDTH_C-1) downto i*BYTE_WIDTH_C), BYTE_WIDTH_C);
```

注意三点：(1) `weaByteInt` 在关闭字节写时把单比特 `wea` 复制到每一位，相当于「整字写」；(2) 每个字节通道独立判断写使能；(3) `resize(..., BYTE_WIDTH_C)` 把（可能不足一个字节的）最高字节补零到满 `BYTE_WIDTH_C`，保证整齐落入 BRAM 字节通道。

读出进程：

[SimpleDualPortRam.vhd:106-117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd#L106-L117) — 同步读，且用与全仓库一致的 `RST_ASYNC_G` 双分支处理复位（异步复位在敏感表 + 沿前，同步复位在沿内）。这与 [u1-l5 双进程风格](u1-l5-two-process-style.md) 的复位约定完全相同。

最后是可选的输出寄存器：

[SimpleDualPortRam.vhd:119-132](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd#L119-L132) — `DOB_REG_G=true` 时再插一拍输出寄存器（可折叠进 BRAM 的内建输出寄存器，改善时序而不增加逻辑资源）。

#### 4.1.4 代码实践

**实践目标：** 用 `SimpleDualPortRam` 搭一个「可字节写的浅 FIFO 存储体」，并通过 `MEMORY_TYPE_G` 控制它被推断成 block RAM 还是 distributed RAM，用注释说明你的预期。

**操作步骤：**

1. 新建一个测试用实体（示例代码，不是仓库原有文件），在内部例化 `SimpleDualPortRam`：

```vhdl
-- 示例代码：可字节写的浅 FIFO 存储体（仅存储阵列，不含读/写指针 FSM）
U_Mem : entity surf.SimpleDualPortRam
   generic map (
      TPD_G         => TPD_G,
      MEMORY_TYPE_G => "block",       -- 选 "block" → 期望推断为块 RAM(BRAM)
                                      -- 改成 "distributed" → 期望推断为 LUT RAM
      BYTE_WR_EN_G  => true,          -- 打开字节写：weaByte 每位对应一个字节写使能
      DATA_WIDTH_G  => 32,            -- 32 位数据字
      BYTE_WIDTH_G  => 8,             -- 8 位/字节 → weaByte 共 4 位
      ADDR_WIDTH_G  => 6)             -- 2^6 = 64 表项，属于“浅”存储
   port map (
      -- Port A：写入侧（写指针驱动 addra）
      clka    => clk,
      wea     => wrEn,                -- 整字写使能（与 weaByte 配合）
      weaByte => wrBe,                -- 4 位字节写使能：x"F" 全写、x"1" 只写最低字节
      addra   => wrPtr,
      dina    => wrData,
      -- Port B：读出侧（读指针驱动 addrb）
      clkb    => clk,
      rstb    => '0',                 -- 本例读口不复位（注意默认值是 not RST_POLARITY_G）
      addrb   => rdPtr,
      doutb   => rdData);
```

2. 在注释里写明：`DATA_WIDTH_G=32`、`BYTE_WIDTH_G=8` 时，`weaByte` 是 4 位；选 `"block"` 时这 4 个字节写使能会一一对应到 BRAM 原生的字节写引脚。
3. 把 `MEMORY_TYPE_G` 分别改成 `"block"` 与 `"distributed"`，各综合一次（见下方「预期结果」）。

**需要观察的现象：**

- 仿真层面（可用 GHDL/cocotb）：写入若干不同字节使能的字，再从对应地址读回，确认只有被 `weaByte` 选中的字节被更新。
- 综合层面（需 Vivado）：在综合后的 **Utilization Report**（资源利用报告）里看 `Block RAM` 与 `Memory LUT` 两项的用量变化。

**预期结果：**

- `MEMORY_TYPE_G => "block"`：资源报告里 `Block RAM` 计数增加，`Memory LUT` 基本不变——推断为块 RAM。
- `MEMORY_TYPE_G => "distributed"`：`Memory LUT` 增加，`Block RAM` 不变——推断为分布式 RAM。

> 「推断成 block 还是 distributed」这一观察**依赖 Vivado 综合**，GHDL 只做仿真不报告推断结果，因此资源利用对照**待本地验证**（需要本地有 Vivado 跑综合）。仿真层面的读写功能则可立即用仓库的 cocotb 栈验证。

#### 4.1.5 小练习与答案

**练习 1：** `DATA_WIDTH_G=12`、`BYTE_WIDTH_G=8`、`BYTE_WR_EN_G=true` 时，`weaByte` 是几位？存储阵列的真实字宽 `FULL_DATA_WIDTH_C` 是多少？

**参考答案：** `NUM_BYTES_C = wordCount(12,8) = 2`，所以 `weaByte` 是 2 位；`FULL_DATA_WIDTH_C = 2×8 = 16`（最高字节只有 4 位有效，靠 `resize` 补齐到 8 位）。

**练习 2：** 为什么 `mem` 要声明成 `shared variable` 而不是普通 `signal`？

**参考答案：** 因为写进程（Port A）和读进程（Port B）是两个独立进程，都要访问同一段存储。VHDL 里普通 `signal` 不能被多个进程写入；`shared variable` 允许双进程共享同一存储体，这正是双口 RAM 的行为模型（综合时再映射到 BRAM 的双口）。

---

### 4.2 真双口 RAM、LUT RAM 与字节写：TrueDualPortRam / DualPortRam / LutRam

#### 4.2.1 概念说明

`SimpleDualPortRam` 的局限是「A 口只能写、B 口只能读」。当两口都需要**独立读写**（各自有独立地址、独立数据输入）时，要用 `TrueDualPortRam`——例如两个 CPU 核共享一片内存、或两套 DMA 通路都要往同一缓冲里读写。

而 `LutRam` 面向另一个极端：**分布式 RAM**，特点是异步读、多读口——适合小容量寄存器堆、查找表。

`DualPortRam` 本身**不实现存储**，它只是一个「路由器」：看 `MEMORY_TYPE_G`，要块就例化 `TrueDualPortRam`，要分布式就例化 `LutRam`，对外暴露统一的两口接口。

#### 4.2.2 核心流程

`TrueDualPortRam` 的每个口都是「读 + 写」复合进程。三种 `MODE_G` 的差别体现在「同址同沿写读」时输出拿到什么。以 Port A 为例，读-写在一个进程里：

```
# read-first 模式
wait until rising_edge(clka);
if ena = '1':
    doutAInt <= mem[addra]            # 先读到旧值
    for 每个字节 i: 若 weaByte(i): mem[addra][i] := dina[i]   # 再写入新值

# write-first 模式
wait until rising_edge(clka);
if ena = '1':
    for 每个字节 i: 若 weaByte(i): mem[addra][i] := dina[i]   # 先写
    doutAInt <= mem[addra]            # 再读到新值

# no-change 模式
wait until rising_edge(clka);
if ena = '1':
    for 每个字节 i: 若 weaByte(i): mem[addra][i] := dina[i]   # 写时输出不动
# （输出在另一个独立进程里，仅当不写时才刷新）
```

`LutRam` 则完全不同：写是同步的，但读是**组合的**——`douta <= mem(conv_integer(addra))` 当拍就出值（当 `REG_EN_G=false` 时）。它还能挂最多 8 个独立读口（B/C/D/E/F/G/H）。

#### 4.2.3 源码精读

**TrueDualPortRam 只推断为 block RAM**——注意文件头注释和硬编码的 `ram_style`：

[TrueDualPortRam.vhd:4](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/TrueDualPortRam.vhd#L4) — 注释明示「This will infer this module as Block RAM only」。

[TrueDualPortRam.vhd:82-83](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/TrueDualPortRam.vhd#L82-L83) — `attribute ram_style of mem : variable is "block";`（注意这里不像 `SimpleDualPortRam` 那样来自泛型，而是写死的 `"block"`）。

实体两口对称，B 口也有 `web`/`webByte`/`dinb`/`doutb`：

[TrueDualPortRam.vhd:24-59](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/TrueDualPortRam.vhd#L24-L59) — 实体声明，注意 A/B 两口结构几乎完全对称（都有写使能、字节写使能、地址、数据输入、数据输出）。

三种模式用三个互斥的 `generate` 实现。先看模式校验断言与字节写使能整形：

[TrueDualPortRam.vhd:100-106](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/TrueDualPortRam.vhd#L100-L106) — `assert` 强制 `MODE_G` 只能是三种合法值之一（否则编译期 `severity failure`）；`weaByteInt`/`webByteInt` 与 4.1 同理。

`read-first` 模式（最常用）——读在写之前：

[TrueDualPortRam.vhd:177-197](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/TrueDualPortRam.vhd#L177-L197) — Port A：先把 `mem[addra]` 读到 `doutAInt`，再做字节写更新 `mem`。这正是 BRAM `read-first` 的标准写法模板，综合器据此映射出对应模式的 BRAM。

对照 `write-first`：

[TrueDualPortRam.vhd:225-246](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/TrueDualPortRam.vhd#L225-L246) — Port A：先写后读，所以 `doutAInt` 拿到的是刚写入的新值。

**DualPortRam 是路由封装**，架构名就叫 `mapping`：

[DualPortRam.vhd:64-98](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/DualPortRam.vhd#L64-L98) — `GEN_BRAM`：当 `MEMORY_TYPE_G /= "distributed"` 时例化 `TrueDualPortRam`，并把 B 口的 `web` 固定接 `'0'`（B 口只读，退化为简单双口的用法）。

[DualPortRam.vhd:100-130](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/DualPortRam.vhd#L100-L130) — `GEN_LUTRAM`：当 `MEMORY_TYPE_G = "distributed"` 时例化 `LutRam`（`NUM_PORTS_G => 2`）。**这就是「按一个泛型切换后端」的典型模式**。

**LutRam 的分布式本质**——读是组合的，且 `mem` 是 `signal`：

[LutRam.vhd:99-117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/LutRam.vhd#L99-L117) — `signal mem`（不是 shared variable）、`ram_style => "distributed"`。注意分布式 RAM 只需单进程写，因为读是组合的。

[LutRam.vhd:130-149](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/LutRam.vhd#L130-L149) — `REG_EN_G=false` 时写进程之外直接 `douta <= mem(conv_integer(addra));`——**异步读，0 拍延迟**。这与 BRAM 的「同步读 1 拍」形成鲜明对比，是选择 distributed 的核心理由。

#### 4.2.4 代码实践

**实践目标：** 通过阅读 `DualPortRam` 理解「一个泛型切换后端」的封装套路，并对照 BRAM 与 LUTRAM 的读时序差异。

**操作步骤：**

1. 打开 [DualPortRam.vhd:64-130](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/DualPortRam.vhd#L64-L130)，画出「`MEMORY_TYPE_G` 取值 → 例化哪个底层块」的判定树。
2. 在 [LutRam.vhd:130-149](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/LutRam.vhd#L130-L149) 与 [SimpleDualPortRam.vhd:106-117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/inferred/SimpleDualPortRam.vhd#L106-L117) 之间做对照，分别标注「给地址后第几拍拿到数据」。

**需要观察的现象 / 预期结果：**

- `DualPortRam` 判定树：`MEMORY_TYPE_G = "distributed"` → `LutRam`；其余任意值（`"block"`/`"auto"`/…）→ `TrueDualPortRam`。
- 读时序：`LutRam`（`REG_EN_G=false`）异步读，地址当拍出数据；`SimpleDualPortRam` 同步读，地址后下一拍出数据。
- 本实践为源码阅读型，**无需运行即可得出结论**。

#### 4.2.5 小练习与答案

**练习 1：** `DualPortRam` 的 `GEN_BRAM` 分支里，B 口的 `web` 为什么接 `'0'`？

**参考答案：** 因为 `DualPortRam` 对外承诺的是「A 口可写、B 口只读」的语义（与 `SimpleDualPortRam` 一致）。它复用了功能更强的 `TrueDualPortRam`，但把 B 口写使能钉死为 `'0'`，让 B 口退化为只读，从而对外行为与简单双口一致。

**练习 2：** 同样是「简单双口」，何时你会选 `DualPortRam`（配 `"distributed"`）而非 `SimpleDualPortRam`？

**参考答案：** 当你既想要简单双口的接口，又明确需要**分布式 RAM 的异步读/0 拍延迟**（或希望它落到 LUT 资源而非占用宝贵的 BRAM）时，用 `DualPortRam` + `MEMORY_TYPE_G="distributed"` 最方便——它内部自动换成 `LutRam`。`SimpleDualPortRam` 虽然也能设 `"distributed"`，但若你想要「一个块名、靠泛型两选一」的统一封装，`DualPortRam` 更直白。

---

### 4.3 inferred 与 XPM/原语封装的选择：从推断到显式原语

#### 4.3.1 概念说明

4.1/4.2 讲的都是 `inferred/`（可推断）路径——我们只写行为模板，让综合器去识别。这套路径**跨厂商、跨工具**（Vivado/Quartus/Genus 都能吃），是默认首选。

但有时推断会「不听话」：综合器可能把一块本该是 BRAM 的存储拆成分布式，或者你想精确控制 BRAM 的 ECC、初始化文件、读延迟等高级参数。这时要改走**显式后端**：

- **XPM 封装（`xilinx/` 下的 `*Xpm`）**：直接例化 Xilinx 提供的 `xpm_memory_*` 宏。XPM 是 Xilinx 官方推荐的、参数化的存储原语，比「裸用 BRB 原语」更可移植、跨 Vivado 版本更稳。
- **原语封装（`SinglePortRamPrimitive`）**：按位直接例化 `RAM32X1S`…`RAM512X1S` 等 unisim 底层原语，最底层、最可控，但最啰嗦。
- **dummy（`dummy/`）**：当目标工具链/版本不支持 XPM 时，提供同名空壳，保证库能通过 elaboration 不报错。

#### 4.3.2 核心流程

选型决策树：

```
需要一个 RAM
 ├─ 默认：用 inferred/（SimpleDualPortRam / TrueDualPortRam / DualPortRam / LutRam）
 │        —— 跨厂商、靠 ram_style 提示综合器
 ├─ 需要精确控制 Xilinx BRAM 参数（ECC、init 文件、读延迟、同/异步时钟显式声明）：
 │        用 xilinx/*Xpm（例化 xpm_memory_sdpram / tdpram）
 └─ 需要绝对底层的位级 LUT RAM 原语（极小深度、确定资源）：
          用 SinglePortRamPrimitive（例化 RAM64X1S 等）
```

构建期，`ruckus.tcl` 决定哪些后端真的进入工程：`inferred/` 永远进；`xilinx/` 仅当 Vivado ≥ 2019.1 才进，否则用 `dummy/` 顶替。

#### 4.3.3 源码精读

**XPM 简单双口封装**——直接例化 `xpm_memory_sdpram`：

[SimpleDualPortRamXpm.vhd:23-24](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SimpleDualPortRamXpm.vhd#L23-L24) — 引入 `library xpm; use xpm.vcomponents.all;`（XPM 宏库）。

[SimpleDualPortRamXpm.vhd:63-83](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SimpleDualPortRamXpm.vhd#L63-L83) — 例化 `xpm_memory_sdpram`，把 SURF 泛型映射到 XPM 参数：`MEMORY_PRIMITIVE => MEMORY_TYPE_G`（block/distributed/ultra）、`MEMORY_SIZE => DATA_WIDTH_G*(2**ADDR_WIDTH_G)`、`CLOCKING_MODE => "common_clock"/"independent_clock"`（由 `COMMON_CLK_G` 决定）、`READ_LATENCY_B => READ_LATENCY_G`（0–100 可调，远比 inferred 灵活）。注意这里**没有 `ram_style` 属性**——因为不再需要「提示综合器」，而是直接点名用哪个底层宏。

真双口的 XPM 封装同理，例化 `xpm_memory_tdpram`：

[TrueDualPortRamXpm.vhd:71-96](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/TrueDualPortRamXpm.vhd#L71-L96) — A/B 两口对称映射到 `xpm_memory_tdpram`，`WRITE_MODE_A/B` 直接由泛型 `WRITE_MODE_G` 控制（对应 4.2 的 `MODE_G`，但交给 XPM 实现）。

**最底层的原语封装**——按位例化 unisim 原语：

[SinglePortRamPrimitive.vhd:4-5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SinglePortRamPrimitive.vhd#L4-L5) — 注释说明手动例化 `RAM32X1S`…`RAM512X1S`。

[SinglePortRamPrimitive.vhd:22-23](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SinglePortRamPrimitive.vhd#L22-L23) — 引入 `library unisim; use unisim.vcomponents.all;`（Xilinx 底层原语库）。

[SinglePortRamPrimitive.vhd:45-125](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SinglePortRamPrimitive.vhd#L45-L125) — 对数据的每一位（`for i in WIDTH_G-1 downto 0`）例化一个 1 位宽的 LUT RAM 原语，按 `DEPTH_G` 选 `RAM32X1S`(≤32)/`RAM64X1S`/`RAM128X1S`/`RAM256X1S`/`RAM512X1S`。注意器件约束：7 系列（`XIL_DEVICE_G="7SERIES"`）最高只到 256，`RAM512X1S` 仅 UltraScale/UltraScale+ 支持——

[SinglePortRamPrimitive.vhd:98-101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SinglePortRamPrimitive.vhd#L98-L101) 与 [SinglePortRamPrimitive.vhd:113](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/xilinx/SinglePortRamPrimitive.vhd#L113) — 用 `assert ... severity failure` 和 `generate` 条件强制这一器件限制。

**ruckus.tcl 的版本门控**——这是把上面所有后端串进构建的总开关：

[ruckus.tcl:5-19](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/ram/ruckus.tcl#L5-L19) — 解读：

- 第 5 行 `loadSource -dir inferred`：**无条件**加载所有可推断 RAM（跨厂商，永远在）。
- 第 8–16 行：当 `VIVADO_VERSION >= 2019.1` 时，加载 `xilinx/`（XPM + 原语），并加载两个 Altera 的 dummy（因为此时是 Xilinx 工具链，Altera 后端用不到，用空壳占位保证库内任何对它们的例化都能 elaboration 通过）；≥2021.2 还会设置 `XPM_LIBRARIES` 属性。
- 第 17–19 行 `else`：老版本 Vivado 不支持 XPM，于是只加载 `dummy/`——保证 SURF 里某处例化了 `*Xpm` 时不会因为缺实体而报错。

这正是 [u1-l3 目录约定](u1-l3-directory-layout.md) 讲过的「用 `ruckus.tcl` + 工具链版本做条件加载」模式在 RAM 子树里的具体落地。

#### 4.3.4 代码实践

**实践目标：** 在真实使用方里定位「谁用了 inferred、谁用了 XPM」，体会选型差异。

**操作步骤：**

1. 打开 FIFO 的同步实现 [FifoSync.vhd:148-155](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd#L148-L155)，确认它用的是 `SimpleDualPortRam`（inferred 路径），并注意 `DOB_REG_G => ite(MEMORY_TYPE_G /= "distributed", FWFT_EN_G, false)`——分布式 RAM 不需要这个输出寄存器。
2. 在仓库内搜索 `SimpleDualPortRamXpm` 的例化点（可用 `grep -r "SimpleDualPortRamXpm" --include=*.vhd`），看看哪些模块为了精确控制 BRAM 而改走了 XPM 路径。

**需要观察的现象 / 预期结果：**

- `FifoSync` 默认走 inferred（与 [u2-l2](u2-l2-fifo-blocks.md) 讲的「统一封装 `Fifo` 再按 `SYNTH_MODE_G` 分流」一致）。
- XPM 的真实例化点数量与具体模块，**待本地验证**（取决于你检索的范围）。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `SimpleDualPortRamXpm` 里**没有** `ram_style` 属性，而 `SimpleDualPortRam`（inferred）里有？

**参考答案：** inferred 路径写的是行为模板，需要用 `ram_style` 属性「提示」综合器该映射成 block 还是 distributed。XPM 路径直接例化了 `xpm_memory_sdpram` 这个明确的底层宏，并通过 `MEMORY_PRIMITIVE` 参数告诉 XPM 用哪种物理资源——不再依赖综合器的推断，所以不需要 `ram_style` 提示。

**练习 2：** 假设团队把工程从 Vivado 2018.3 升级到 2022.1，`base/ram/ruckus.tcl` 的行为会发生什么变化？

**参考答案：** 2018.3 走 `else` 分支，只加载 `inferred/` + `dummy/`（任何 `*Xpm` 例化都落到 dummy 空壳）。升级到 2022.1 后，进入 `if` 分支：加载真正的 `xilinx/`（XPM + 原语生效），并额外设置 `XPM_LIBRARIES XPM_MEMORY` 属性——此前用 dummy 占位的 XPM 例化点现在会真正实现成 BRAM。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务：

**任务：为一个「按字节更新的小型配置缓存」选型并搭出存储体。**

需求：

- 容量 32 个表项，每项 24 位；CPU 侧需要按 8 位字节单独改写某一项。
- 另一侧（数据通路）需要随时读当前值，且希望读到的是「当拍地址」对应的数据（接受 0 拍或 1 拍读延迟两种方案，请各给出一版）。

要求你完成：

1. **选型论证**：根据容量（小）与「按字节写」需求，判断该用 `SimpleDualPortRam` 还是 `LutRam`/`DualPortRam`；分别给出 block RAM 版与 distributed RAM 版的泛型取值。
2. **字节写宽度计算**：手算 `DATA_WIDTH_G=24, BYTE_WIDTH_G=8` 时的 `NUM_BYTES_C`、`FULL_DATA_WIDTH_C` 与 `weaByte` 位数。
3. **接线**：仿照 4.1.4 的示例代码写出一版 `SimpleDualPortRam` 例化（`ADDR_WIDTH_G=5`，因 2^5=32），并在注释里写明它会被推断成 block RAM；再写一版用 `DualPortRam` + `MEMORY_TYPE_G="distributed"` 走分布式路径。
4. **读时序对照**：用一句话说明两版在「给地址后第几拍拿到数据」上的差异，并指出这对应 4.2.3 里 `LutRam` 的异步读与 `SimpleDualPortRam` 的同步读。

**参考要点（自检用）：**

- 选型：小容量 + 字节写，两者都支持字节写；若要 0 拍异步读选 distributed（`LutRam`/`DualPortRam`），若要更大密度/省 LUT 选 block（`SimpleDualPortRam`）。
- 计算：`NUM_BYTES_C = wordCount(24,8) = 3`；`FULL_DATA_WIDTH_C = 3×8 = 24`；`weaByte` 为 3 位。
- 读时序：distributed 版（`REG_EN_G=false`）当拍出数据；block 版下一拍出数据。

> 综合/资源层面的结论（推断成 block 还是 distributed、LUT/BRAM 用量变化）依赖 Vivado，**待本地验证**；字节写功能本身可用仓库的 cocotb/GHDL 栈仿真验证。

## 6. 本讲小结

- SURF 的 RAM 块分两步用：先选**拓扑**（简单双口 `SimpleDualPortRam` / 真双口 `TrueDualPortRam` / 分布式多读口 `LutRam` / 路由封装 `DualPortRam`），再选**后端**（inferred / XPM / 原语 / dummy）。
- 「推断」靠 `shared variable mem` + `attribute ram_style => "block"|"distributed"` 提示综合器；`SimpleDualPortRam` 把这个值做成泛型 `MEMORY_TYPE_G`，`TrueDualPortRam` 则写死 `"block"`。
- **字节写**用 `weaByte`（每字节一位）向量实现，`wordCount(DATA_WIDTH_G, BYTE_WIDTH_G)` 决定其位数；`FULL_DATA_WIDTH_C` 可能大于 `DATA_WIDTH_G` 以整齐对齐 BRAM 字节通道（含奇偶位）。
- BRAM **同步读**（≥1 拍）、容量大、两口；distributed（`LutRam`）**异步读**（可 0 拍）、多读口、容量小——这是选型的核心权衡。
- `TrueDualPortRam` 用三个 `generate` 实现 `no-change`/`read-first`/`write-first` 三种同址写读模式；`DualPortRam` 按一个 `MEMORY_TYPE_G` 在块与分布式之间二选一。
- `ruckus.tcl` 无条件加载 `inferred/`，并按 `VIVADO_VERSION >= 2019.1` 门控 `xilinx/`（XPM）的加载，否则用 `dummy/` 占位——这是 SURF 处理「厂商/版本相关后端」的通用手法。

## 7. 下一步学习建议

- **回头印证 FIFO**：本讲的 `SimpleDualPortRam` 正是 [u2-l2 FIFO 构建块](u2-l2-fifo-blocks.md) 里 `FifoSync`/`FifoAsync` 的存储体。建议重读 [FifoSync.vhd:148-155](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/fifo/rtl/inferred/FifoSync.vhd#L148-L155)，看 FIFO 的读写指针如何驱动 `SimpleDualPortRam` 的 A/B 口地址。
- **向上看内存映射**：`AxiDualPortRam`（在 `axi/axi-lite/`）把一块双口 RAM 挂到 AXI-Lite 总线上，是 [u3 AXI-Lite 寄存器层](u3-l4-axiversion-helpers.md) 的核心组件之一，它会复用本讲的 RAM 块。
- **看 XPM 的更高级用法**：当你学到 [u8 DMA](u8-l2-axistream-dma-v2.md) 时，会看到 DMA 缓冲为了精确控制 BRAM 的读延迟与 ECC，往往改走 `*Xpm` 路径——届时可回看本讲 4.3。
- **动手验证**：在本地用 Vivado 综合一段 `SimpleDualPortRam` 例化，对照 `MEMORY_TYPE_G` 取 `"block"` 与 `"distributed"` 时的 Utilization Report，亲手确认本讲多处标注的「待本地验证」结论。
