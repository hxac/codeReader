# 片上 RAM 抽象：ocram 家族

## 1. 本讲目标

本讲聚焦 `src/mem/ocram/` 命名空间，带你理解 PoC 如何把 FPGA 上的「片上 RAM（On-Chip RAM，ocram）」抽象成一组可移植、可配置的 IP 核。

学完后你应当能够：

- 区分 `ocram_sp` / `ocram_sdp` / `ocram_esdp` / `ocram_tdp` 以及 `_wf`（write-first）等存储器配置，知道它们的端口拓扑与适用场景。
- 理解「通用 ocram 实体」如何用一段厂商无关的 RTL 数组让综合器**推断（infer）**出 Block RAM，并掌握「写优先（write-first）」与「无所谓（don't-care）」两种读-写冲突行为的编码写法差异。
- 看懂 ocram 如何在 `if ... generate` 中依据 `VENDOR` 在「RTL 推断」与「厂商原语实例化」之间分发，特别是 Altera 分支如何调用底层 `altsyncram` 原语。

本讲是上一讲 [u3-l2 厂商选择与可移植机制](u3-l2-vendor-selection-portability.md) 的延伸——那里用 `sync_Bits` 讲了「双层选择」框架，这里把同一套框架套用到一类更复杂的资源（存储器）上，并增加「读-写冲突行为」这一个新的设计维度。

---

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（由 u1、u2、u3 前序讲义提供）：

- **命名空间包模式**（u3-l1）：每个命名空间有一份 `<ns>.pkg.vhdl` 根包，集中声明组件、类型与函数；ocram 的组件声明就在 `ocram.pkg.vhdl` 里。
- **厂商选择与双层可移植机制**（u3-l2）：包装实体用 `if ... generate` 在 `gGeneric` / `gAltera` / `gXilinx` 等分支间展开；pyIPCMI 在 `.files` 里按 `DeviceVendor` 做**编译时**选择，VHDL `generate` 做**展开期**选择，两层都由同一份 `MY_DEVICE` 驱动。`config.vhdl` 把器件字符串解析成 `T_VENDOR` 枚举（`VENDOR_XILINX` / `VENDOR_ALTERA` / `VENDOR_LATTICE` / `VENDOR_GENERIC`）。
- **公共包 utils / vectors / strings**（u2-l2、u2-l4）：`ite`（编译期三元运算）、`is_x`、`SIMULATION` 常量、`T_SLM`（二维位矩阵）等都会在本讲再次出现。
- **FPGA 存储器基础**：Block RAM（BRAM）是 FPGA 内置的专用存储块，读写都「钟控（synchronous）」——数据在时钟沿之后的一个周期才出现在输出端。综合器通常能从一段「数组 + 钟控进程」的 RTL **推断**出 BRAM，但各厂商推断能力不同，推断不出时就要直接实例化厂商原语（primitive）。

如果你对下面两个术语不熟，先记住一句话解释：

- **端口（port）**：存储器对外的一个访问通道，含独立的数据/地址/控制信号。端口数决定存储器是「单端口」还是「双端口」。
- **读-写冲突（Read-During-Write，RDW）**：同一时刻对同一地址又读又写时，读端口到底拿到「新写入的值」「旧的值」还是「不确定的值」——这正是本讲要讲清楚的「存储行为差异」。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下（路径相对仓库根）：

| 文件 | 作用 |
| --- | --- |
| [src/mem/ocram/ocram.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl) | ocram 命名空间根包：集中声明 `ocram_sp/sdp/esdp/tdp` 四个组件。 |
| [src/mem/ocram/ocram_sp.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl) | 单端口存储器：通用包装实体，含 `gInfer`（RTL 推断）与 `gAltera`（实例化子实体）两条分支。 |
| [src/mem/ocram/ocram_sdp.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sdp.vhdl) | 简单双端口存储器（1 读端口 + 1 写端口，双时钟），含 `gInfer` 与 `gSim` 分支。 |
| [src/mem/ocram/ocram_tdp.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_tdp.vhdl) | 真双端口存储器（2 个读写端口），三条分支齐全（`gInfer` / `gAltera` / `gSim`）。 |
| [src/mem/ocram/ocram_esdp.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_esdp.vhdl) | 增强型简单双端口（1 读写端口 + 1 读端口），**自 1.1 起废弃**，新设计请用 `ocram_tdp`。 |
| [src/mem/ocram/altera/ocram_sp_altera.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl) | Altera 专用单端口实体：直接实例化 `altsyncram` 原语。 |
| [src/mem/ocram/ocram_tdp_sim.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_tdp_sim.vhdl) | 仿真模型：精确模拟读-写冲突行为与 X 传播，仅在仿真时编译。 |
| [src/mem/mem.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/mem.pkg.vhdl) | `PoC.mem` 根包：提供内存初始化文件格式枚举与 `mem_ReadMemoryFile` 等函数，被 ocram 复用。 |
| `src/mem/ocram/*.files` | 各核的编译清单，规定 pyIPCMI 在什么厂商/环境下编译哪些文件。 |

> 小地图记忆法：`mem.pkg.vhdl`（根包，提供文件读取能力）→ `ocram.pkg.vhdl`（组件货架）→ `ocram_*.vhdl`（各配置的包装实体）→ `altera/ocram_*_altera.vhdl`（厂商专用子实体）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **存储器配置类型**——ocram 家族有哪些成员，端口拓扑如何。
2. **通用 ocram 实体**——RTL 推断模型与读-写冲突行为的编码差异。
3. **厂商实例化**——`generate` 分发与 `altsyncram` 原语对接。

---

### 4.1 存储器配置类型：ocram 家族矩阵

#### 4.1.1 概念说明

FPGA 设计里「存储器」需求五花八门：有时只需要一个端口顺序读写（像一个小缓存），有时需要一边写一边读（像 FIFO 的底层存储），有时两个主设备都要读写同一块内存（像 CPU 与 DMA 共享内存）。这些需求差异归结为两个维度：

- **端口数**：1 个还是 2 个访问通道。
- **每个端口的能力**：只能读、只能写、还是既能读又能写。
- **时钟数**：所有端口共用一个时钟，还是读写各自独立时钟（跨时钟域）。

PoC 把这些组合预先做成了一组核，统一冠以 `ocram_`（on-chip RAM）前缀。它们的 generic/port 接口高度一致：地址位宽 `A_BITS`、数据位宽 `D_BITS`、初始化文件名 `FILENAME`，存储深度由 `A_BITS` 决定：

\[
\text{DEPTH} = 2^{\text{A\_BITS}}, \qquad \text{A\_BITS} = \lceil \log_2(\text{DEPTH}) \rceil
\]

这和 [u2-l2](u2-l2-utils-package.md) 讲过的 `log2ceil` 是同一个映射（FIFO 用它算指针位宽，ocram 直接用 `2**A_BITS` 反推深度）。

#### 4.1.2 核心流程

下表是 ocram 家族的「选型矩阵」。读完它，你就能根据「几个端口 / 谁能写 / 几个时钟」挑出合适的核。

| 核 | 端口拓扑 | 时钟 | 读-写冲突行为 | 典型用途 |
| --- | --- | --- | --- | --- |
| `ocram_sp` | 1 个读写端口 | 单时钟 | 写优先（write-first） | 小型查找表、单主设备缓存 |
| `ocram_sdp` | 1 写端口 + 1 读端口 | 双时钟（wclk/rclk） | 无所谓（don't-care，混口） | **FIFO 底层存储**、跨时钟域缓冲 |
| `ocram_sdp_wf` | 1 写端口 + 1 读端口 | 单时钟 | 写优先（write-first） | 同钟下需要立即读到新值的缓冲 |
| `ocram_esdp` | 1 读写端口 + 1 读端口 | 双时钟 | 同口写优先 / 混口无所谓 | 旧设计兼容（**已废弃**，改用 `ocram_tdp`） |
| `ocram_tdp` | 2 个读写端口 | 双时钟 | 同口写优先 / 混口无所谓 | CPU + DMA 共享内存、双主设备 |
| `ocram_tdp_wf` | 2 个读写端口 | 单时钟 | 写优先 | 同钟双主设备 |

几个关键决策点：

- **要不要跨时钟域？** 要 → 选 `sdp`/`tdp`（双时钟）；不要 → 选 `sp` 或 `*_wf`（单时钟）。
- **要不要第二个端口也能写？** 要 → `tdp`（真双端口）；不要 → `sdp`（一读一写）或 `sp`（单端口）。
- **读写冲突时要确定值还是允许不确定？** 需要确定 → `*_wf`（写优先，会额外产生旁路逻辑）；允许不确定（综合器更省资源）→ 普通 `sdp`/`tdp`。

> 名词解释：**同口（same-port）冲突**指同一个端口既写又读同一地址；**混口（mixed-port）冲突**指端口 1 写、端口 2 读同一地址。真双端口 RAM 这两种冲突的行为可以不同——`ocram_tdp` 就是「同口写优先、混口无所谓」。

#### 4.1.3 源码精读

ocram 家族的「目录页」是 [ocram.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl)。它声明了四个核心组件，每个组件的 generic/port 都遵循上面说的一致约定。看单端口的声明：

[ocram.pkg.vhdl:41-55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl#L41-L55) 声明 `ocram_sp`：`A_BITS`/`D_BITS`/`FILENAME` 三个 generic，端口只有一个 `clk`/`ce`/`we`/`a`/`d`/`q`——典型的单端口。

[ocram.pkg.vhdl:57-74](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl#L57-L74) 声明 `ocram_sdp`：注意它有**两个地址** `ra`/`wa` 和**两个时钟** `rclk`/`wclk`，但没有读使能/写使能之外的额外控制——这正是「一读一写」的拓扑。

[ocram.pkg.vhdl:96-116](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl#L96-L116) 声明 `ocram_tdp`：两套完全对称的 `clk1/ce1/we1/a1/d1/q1` 与 `clk2/ce2/we2/a2/d2/q2`——两个端口都能读能写。

注意一个细节：**根包只声明了 `sp/sdp/esdp/tdp` 四个组件，没有声明 `_wf` 变体**。也就是说 `_wf` 系列没有进入命名空间包的「公共货架」，使用时需要直接实例化实体，而不是通过包里的 component 声明。

`ocram_esdp` 虽然仍在包里声明（[ocram.pkg.vhdl:76-94](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl#L76-L94)），但它的源码头明确标注了废弃：

[ocram_esdp.vhdl:17-21](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_esdp.vhdl#L17) `.. deprecated:: 1.1`，提示「新设计请用 `ocram_tdp`」——它存在的原因是早期综合器无法从 RTL 推断真双端口 RAM，现在已不需要。

最后，所有 ocram 核都依赖上层 `PoC.mem` 根包提供的初始化文件读取能力。[mem.pkg.vhdl:62-82](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/mem.pkg.vhdl#L62-L82) 定义了文件格式枚举（Intel Hex / Lattice Mem / Xilinx Mem）与 `mem_ReadMemoryFile` 函数——这是「根包共享层 + 家族子包」二级拆分的典型例子（见 [u3-l1](u3-l1-namespace-package-pattern.md)）。

#### 4.1.4 代码实践

**实践目标**：通过阅读组件声明，练习「按需求选型」。

**操作步骤**：

1. 打开 [ocram.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl)。
2. 假设你要为 [u3-l4 FIFO 家族](u3-l4-fifo-family.md) 里的**跨时钟域 FIFO**（`fifo_ic_got`）挑选底层存储：写入时钟域和读出时钟域不同，且永远是一边写、一边读，不会两个端口同时写。你会选哪个 ocram？
3. 再假设你要做一块「CPU 和 DMA 共享」的内存，两边都要读写，选哪个？

**需要观察的现象**：跨时钟域 FIFO 的存储要求「一写一读、双时钟」，正好匹配 `ocram_sdp` 的拓扑（`wa`/`ra` 双地址、`wclk`/`rclk` 双时钟）。事实上 PoC 的 FIFO 正是实例化 `ocram_sdp` 作为底层存储的（[u3-l4](u3-l4-fifo-family.md) 会展开）。

**预期结果**：跨时钟域 FIFO → `ocram_sdp`；CPU+DMA 共享内存 → `ocram_tdp`。如果你选了 `ocram_sp`，说明忽略了「跨时钟域」这个约束。

#### 4.1.5 小练习与答案

**练习 1**：`ocram_sdp` 和 `ocram_sdp_wf` 都是「一写一读」，区别在哪？

> **答案**：时钟数与读-写冲突行为。`ocram_sdp` 双时钟（`wclk`/`rclk`），混口读-写冲突为「无所谓（don't-care）」；`ocram_sdp_wf` 单时钟，混口冲突为「写优先（write-first）」，会多出旁路逻辑。

**练习 2**：为什么 `ocram_esdp` 被废弃？

> **答案**：它存在是为了绕开早期综合器无法推断真双端口 RAM 的限制；现在 `ocram_tdp` 已经能被正确推断，`esdp`（1 读写端口 + 1 读端口）成了多余的特例，源码以 `.. deprecated:: 1.1` 显式提示改用 `ocram_tdp`。

---

### 4.2 通用 ocram 实体：RTL 推断模型与读写行为

#### 4.2.1 概念说明

「通用 ocram 实体」指 ocram 各核里那段**厂商无关**的 RTL 实现。它的核心思想是：用一段「数组类型 + 钟控进程」描述存储器的行为，让综合器自己把它**推断（infer）**成 BRAM——而不是写死某个厂商的原语。推断的好处是一份代码多厂商通用；代价是不同综合器的推断能力不同，行为也可能有微妙差异。

这里有一个本讲最关键的技术点：**读-写冲突（RDW）行为完全由 RTL 的写法决定**。同一块 BRAM，三种经典写法对应三种行为：

- **写优先（write-first）**：读写同一地址时，读端口拿到「新写入的值」。写法是「寄存**地址**、组合读数组」。
- **读优先（read-first）/ NO_CHANGE**：读端口拿到「旧值」。写法是「钟控进程里 `q <= ram(addr)`」，即寄存**数据**。
- **无所谓（don't-care）**：读端口值不确定。写法是「读写分属不同钟控进程/不同端口」。

PoC 的 ocram 家族主要提供「写优先」（`sp`、`*_wf`）和「无所谓」（`sdp`、`tdp` 的混口）两种行为，没有单独的「读优先」核——这是设计取舍，因为 FIFO 等典型场景要么要写优先、要么允许 don't-care。

#### 4.2.2 核心流程

通用 ocram 实体的内部结构可以概括为四步：

1. **派生深度**：`constant DEPTH : positive := 2**A_BITS;`
2. **定义存储数组**：`type ram_t is array(0 to DEPTH-1) of std_logic_vector(D_BITS-1 downto 0);`
3. **可选地加载初始化文件**：调用 `mem_ReadMemoryFile`（来自 `PoC.mem`）把 Intel Hex 或 Xilinx Mem 文件读进数组初值。
4. **钟控进程描述读写**：根据期望的 RDW 行为选择编码风格。

写优先与无所谓的编码差异，用伪代码对比最清楚：

```
-- 写优先（ocram_sp / ocram_sdp_wf 的写法）
process(clk)
  if rising_edge(clk) then
    if we='1' then ram(a) <= d; end if;   -- 写
    a_reg <= a;                            -- 寄存「地址」
  end if;
end process;
q <= ram(to_integer(a_reg));               -- 组合读 → 读到新值

-- 无所谓（ocram_sdp 的写法，读写分进程）
process(wclk)  if rising_edge(wclk) and (wce and we)='1' then ram(wa)<=d;  -- 写进程
process(rclk)  if rising_edge(rclk) and rce='1'          then q<=ram(ra);  -- 读进程（寄存数据）
```

写优先的关键技巧是「**寄存地址，再组合读数组**」。因为写操作 `ram(a) <= d` 和地址寄存 `a_reg <= a` 在同一个时钟沿发生，沿之后 `a_reg` 等于刚被写入的地址 `a`，而组合读 `ram(a_reg)` 读到的是已经更新的数组——于是输出就是新值，即写优先。

#### 4.2.3 源码精读

先看 `ocram_sp` 的通用实现分支。它的实体声明见 [ocram_sp.vhdl:68-82](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L68-L82)：generic 为 `A_BITS`/`D_BITS`/`FILENAME`，端口为单端口的 `clk/ce/we/a/d/q`。

[ocram_sp.vhdl:90-94](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L90-L94) 是 `gInfer` 分支的入口与数组定义。注意守卫条件：`(VENDOR = VENDOR_GENERIC) or (VENDOR = VENDOR_LATTICE) or (VENDOR = VENDOR_XILINX)`——也就是说 Generic、Lattice、Xilinx 三家**都走推断**，只有 Altera 走另一条路（下一节讲）。

初始化文件加载是一个 `impure function`：

[ocram_sp.vhdl:97-116](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L97-L116) `ocram_InitMemory`：若 `FILENAME` 为空，返回全 0（仿真时为 `'U'`）；否则按文件扩展名 `.mem`（Xilinx Mem 格式）或其它（Intel Hex）调用 `mem_ReadMemoryFile` 读进 `T_SLM` 矩阵，再转成 `ram_t` 数组。注意 L103 那句 `ite(SIMULATION, 'U', '0')`——仿真时初始化为 `'U'` 以暴露未初始化读取，综合时为 `'0'`。

核心的钟控进程与输出：

[ocram_sp.vhdl:122-136](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L122-L136) ——这就是上文说的「写优先」编码：进程内 `if we='1' then ram(a)<=d;` 紧跟 `a_reg <= a;`，输出 `q <= ram(to_integer(a_reg))` 是组合读。L135 还有一句 `(others => 'X') when SIMULATION and is_x(...)`，在仿真里对未初始化地址输出 `'X'` 以加速发现 bug。

再对比 `ocram_sdp` 的「无所谓」写法：

[ocram_sdp.vhdl:133-149](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sdp.vhdl#L133-L149) ——写进程敏感 `wclk`、读进程敏感 `rclk`，读进程里直接 `q <= ram(to_integer(ra))`（寄存**数据**而非地址）。两个进程作用于同一数组但时钟独立，综合器无法保证混口冲突时的值，于是行为就是「无所谓」。

`ocram_sdp` 还在推断分支里加了一条 Altera 专用属性：

[ocram_sdp.vhdl:102-130](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sdp.vhdl#L102-L130) 定义 `attribute ramstyle`，并在 L130 把它绑到 `ram` 信号为 `"no_rw_check"`——告诉 Quartus「不要为读-写冲突加旁路逻辑」，即接受 don't-care 行为。这是「能用属性微调就不必实例化原语」的折中。

`ocram_sp` 的源码注释也明确点出了写优先行为及其依据：

[ocram_sp.vhdl:31-35](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L31-L35) 说明「写时输出会在下一周期给出新数据，即 write-first」，并引用了 Altera Stratix 5 手册作为依据（同时指出另一份 Altera 文档此处写错了）。

最后，关于「写优先」编码风格为何成立，`ocram_sdp_wf` 的实现笔记里有一段非常坦诚的多工具实测记录，值得一看：

[ocram_sdp_wf.vhdl:75-114](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sdp_wf.vhdl#L75-L114) 作者把「寄存地址 + 异步读」写法分别交给 Quartus、Lattice、XST、Vivado 综合，记录每家是否正确加了旁路逻辑——这正说明「读-写冲突行为」是各厂商表现不一的雷区，也是 PoC 要为 Altera 单独做原语实例化的根因。

#### 4.2.4 代码实践

**实践目标**：通过静态阅读，验证「寄存地址 vs 寄存数据」如何决定 RDW 行为。

**操作步骤**：

1. 打开 [ocram_sp.vhdl:122-136](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L122-L136)。
2. 假设在某时钟沿 `a=5`、`we='1'`、`d=0xAA`，且上一沿 `a_reg=5`。沿之后 `ram(5)` 变成什么？`q` 又是什么？
3. 对比 [ocram_sdp.vhdl:142-149](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sdp.vhdl#L142-L149)：若 `rclk` 与 `wclk` 同频同相、`ra=wa=5` 且同时发生写，`q` 能否保证等于新值？

**需要观察的现象**：

- `ocram_sp`：沿后 `ram(5)=0xAA`，`a_reg=5`，组合读 `q=ram(5)=0xAA` → 拿到新值（写优先）。
- `ocram_sdp`：写进程和读进程独立，`q <= ram(ra)` 寄存的是「沿之前」的旧值；两个进程对同一数组的更新顺序在仿真里未定义 → 行为不确定（无所谓）。

**预期结果**：你能用自己的话解释「为什么寄存地址得到写优先、寄存数据得到无所谓」。如果还不确定，把 `a_reg <= a` 删掉、改成 `q <= ram(to_integer(a))`（纯组合），想想综合器还会不会把它推断成 BRAM（答案：不会，会变成分布式 RAM 或 LUT）。

> 待本地验证：上述沿后取值行为可在 GHDL/ModelSim 里写一个最小 testbench 实测确认。

#### 4.2.5 小练习与答案

**练习 1**：`ocram_sp` 里为什么要单独保留一个 `a_reg` 信号，而不是直接 `q <= ram(to_integer(a))`？

> **答案**：直接组合读 `ram(a)` 会让综合器把它推断成分布式 RAM（LUTRAM）而非 BRAM，且失去「钟控读」的一拍延迟。用 `a_reg` 寄存地址、再组合读数组，既保留了钟控语义（输出延迟一拍），又因为「写地址 = 寄存地址」而得到写优先行为，综合器能正确映射到 BRAM。

**练习 2**：`ocram_InitMemory` 里 `ite(SIMULATION, 'U', '0')` 的作用是什么？

> **答案**：仿真时把未初始化单元填成 `'U'`，让任何未初始化读取在波形里立即可见（便于查 bug）；综合时填 `'0'`，因为真实 BRAM 上电值是确定的 0（或厂商指定值）。`SIMULATION` 是 [u2-l2](u2-l2-utils-package.md) 讲过的延迟常量。

---

### 4.3 厂商实例化：generate 分发与原语对接

#### 4.3.1 概念说明

4.2 节的「推断」写法并非万能：某些厂商的综合器无法从 RTL 正确推断出某种存储器配置，或推断出的读-写行为不符合预期。这时就需要**直接实例化厂商原语**。PoC 沿用 [u3-l2](u3-l2-vendor-selection-portability.md) 讲过的「双层选择」框架来组织这件事：

- **展开期（VHDL `generate`）**：包装实体（如 `ocram_sp`）用 `if VENDOR = ... generate` 在 `gInfer`（RTL 推断）与 `gAltera`（实例化厂商子实体）之间二选一。
- **编译期（pyIPCMI `.files`）**：`.files` 清单按 `DeviceVendor` 决定**编译哪些文件**——只有 Altera 目标才会编译 `altera/ocram_sp_altera.vhdl` 并引入 `altera_mf` 原语库；只有仿真环境才会编译 `ocram_tdp_sim.vhdl`。

两层由同一份 `MY_DEVICE`（经 `config.vhdl` 解析成 `VENDOR`）驱动，保证展开期与编译期一致。

#### 4.3.2 核心流程

一个 ocram 包装实体的内部分发逻辑（以最完整的 `ocram_tdp` 为例）有三条分支：

```
if not SIMULATION and (VENDOR in {LATTICE, XILINX})  → gInfer  : RTL 推断
if not SIMULATION and (VENDOR = ALTERA)              → gAltera : 实例化 ocram_tdp_altera
if SIMULATION                                        → gSim    : 实例化 ocram_tdp_sim（仿真模型）
assert (支持的厂商组合) severity failure               → 未覆盖厂商直接报错
```

要点：

- `SIMULATION` 守卫把仿真模型与综合路径彻底隔开——仿真永远用精确定义的 `ocram_tdp_sim`，综合永远用推断或原语。这避免了「仿真和综合行为不一致」的经典陷阱。
- 末尾的 `assert ... severity failure` 是兜底：如果用户给了一个未适配的厂商（比如某厂商既不在推断列表也不在原语列表），elaboration 时直接报错，而不是静默生成错误硬件。这正是 [u3-l2](u3-l2-vendor-selection-portability.md) 讲过的「未覆盖厂商用 `assert failure` 兜底」策略。
- 编译期 `.files` 里，`if (DeviceVendor = "Altera")` 决定是否引入 `lib/Altera.files`（即 `altera_mf` 库）和厂商子实体源码——如果展开期用了 `gAltera` 但编译期没编译 `ocram_sp_altera.vhdl`，就会链接失败。

#### 4.3.3 源码精读

先看 `ocram_sp` 的分发。4.2 节已看过 `gInfer`，现在看 `gAltera`：

[ocram_sp.vhdl:139-172](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L139-L172) `gAltera` 分支：当 `VENDOR = VENDOR_ALTERA` 时，实例化子实体 `ocram_sp_altera`，把同样的 generic/port 透传过去。注释（L155-157）解释了为何不直接在包装实体里实例化 `altsyncram`——ModelSim 需要 `altera_mf` 库的配合，多包一层子实体更稳妥。

兜底的 assert 在这里：

[ocram_sp.vhdl:174-176](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L174-L176) 列出全部已支持厂商，否则 `severity failure`。

再看 Altera 子实体如何对接原语。这是本讲的实践重点：

[ocram_sp_altera.vhdl:49-63](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L49-L63) `ocram_sp_altera` 的 entity，generic/port 与包装实体完全一致（这样才能无缝替换）。

[ocram_sp_altera.vhdl:66-93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L66-L93) 在架构体内部**声明** Altera 原语 `altsyncram` 的 component——注意它要 `library altera_mf; use altera_mf.all;`（见文件 L40-41）。

[ocram_sp_altera.vhdl:103-128](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L103-L128) 真正实例化 `altsyncram`。关键 generic 映射：

- `operation_mode => "SINGLE_PORT"`（L112）——告诉原语工作在单端口模式。
- `width_a => D_BITS`、`widthad_a => A_BITS`、`numwords_a => DEPTH`（L111/L116/L117）——位宽、地址位宽、深度。
- `init_file => INIT_FILE`（L107），其中 `INIT_FILE` 在 L96 由 `ite(str_length(FILENAME)=0, "UNUSED", FILENAME)` 决定——无初始化文件时填 `"UNUSED"`。
- `intended_device_family => getAlteraDeviceName(DEVICE)`（L108）——把 `config.vhdl` 解析出的 `DEVICE` 转成 Quartus 认识的器件族名。
- `outdata_reg_a => "UNREGISTERED"`（L114）——输出不寄存，对应通用实现的「组合读」语义。

端口映射里 `clocken0=>ce`、`wren_a=>we`、`clock0=>clk`、`address_a=>a_sl`、`data_a=>d`、`q_a=>q`，把 ocram 的 `ce/we/clk/a/d/q` 一一对应到原语引脚（地址先在 L101 转成 `std_logic_vector`，因为原语要 SLV 而非 unsigned）。

对比一下：`ocram_sdp` 对 Altera 走的是另一条路——**不实例化原语**，而是用 `ramstyle` 属性（4.2.3 节）让 Quartus 推断。所以是否实例化原语，取决于「该厂商能否正确推断这种配置」：

- `ocram_sp`：Quartus 推断不出正确的单端口 → **实例化** `altsyncram`。
- `ocram_sdp`：Quartus 能推断（加 `no_rw_check`）→ **不实例化**，用属性微调。
- `ocram_tdp`：Quartus 推断不出真双端口 → **实例化** `ocram_tdp_altera`。

最后看编译期选择。`ocram_sp.files`：

[ocram_sp.files:14-19](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.files#L14-L19) 先编译 `ocram.pkg.vhdl`；`if (DeviceVendor = "Altera")` 时引入 `lib/Altera.files`（`altera_mf` 库）和 `ocram_sp_altera.vhdl`；最后才编译 `ocram_sp.vhdl`。顺序很重要：子实体必须先于包装实体编译，否则包装实体里的 `component ocram_sp_altera` 声明找不到对应实体（详见 [u3-l1](u3-l1-namespace-package-pattern.md) 的编译顺序规则）。

`ocram_tdp.files` 进一步展示了仿真分支：

[ocram_tdp.files:15-22](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_tdp.files#L15-L22) `if (Environment = "Simulation")` 时编译 `ocram_tdp_sim.vhdl`——这对应 `ocram_tdp` 里 `gSim` 分支实例化的仿真模型。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：比较 `ocram_sp` 的通用实现与 `ocram_sp_altera`，说明 Altera 分支调用了哪个底层原语，并解释为何要单独存在这个子实体。

**操作步骤**：

1. 打开通用实现 [ocram_sp.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl)，确认 `gAltera` 分支（[L139-172](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L139-L172)）实例化的子实体名。
2. 打开 [altera/ocram_sp_altera.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl)，找到它实例化的原语（[L103](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L103)），以及该原语来自哪个库（[L40-41](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L40-L41)）。
3. 读子实体头部注释 [ocram_sp_altera.vhdl:12-16](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L12-L16)，回答「为什么要直接实例化原语」。
4. 对照 [ocram_sp.files:15-18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.files#L15-L18)，说明编译期是如何配合的。

**需要观察的现象 / 预期结果**：

- `ocram_sp` 的 `gAltera` 分支实例化的子实体是 **`ocram_sp_altera`**。
- `ocram_sp_altera` 内部实例化的底层原语是 **`altsyncram`**（Altera 的通用存储器宏功能），来自 **`altera_mf`** 库，工作模式 `operation_mode => "SINGLE_PORT"`。
- 存在原因：**Quartus 无法从 RTL 正确推断这种单端口 RAM**（头部注释原话「Quartus synthesis does not infer this RAM type correctly」），所以必须直接实例化 `altsyncram`。而多包一层 `ocram_sp_altera` 子实体（而不是在包装实体里直接实例化原语）是为了满足 ModelSim 对 `altera_mf` 库的引用要求。
- 编译期配合：`.files` 仅在 `DeviceVendor = "Altera"` 时引入 `lib/Altera.files`（`altera_mf` 库）并编译 `ocram_sp_altera.vhdl`，且**先于** `ocram_sp.vhdl` 编译。

把这个调用链画出来就是：

```
ocram_sp (包装实体)
   ├── gInfer   (GENERIC/LATTICE/XILINX) → RTL 数组推断
   └── gAltera  (ALTERA)
           └── ocram_sp_altera (子实体)
                   └── altsyncram  (altera_mf 原语, SINGLE_PORT)
```

> 待本地验证：若有 Quartus 环境，可分别用 `MY_DEVICE` 设成 Xilinx 与 Altera 型号综合 `ocram_sp`，对比前者推断出的 BRAM 与后者实例化的 `altsyncram`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ocram_sp` 要实例化 `ocram_sp_altera`，而 `ocram_sdp` 对 Altera 却不实例化原语？

> **答案**：Quartus 能正确推断 `ocram_sdp`（配合 `ramstyle = "no_rw_check"` 属性接受 don't-care 行为），所以走推断；但推断不出 `ocram_sp` 的单端口配置（或行为不符合预期），所以必须实例化 `altsyncram`。是否实例化原语，取决于该厂商能否正确推断该配置。

**练习 2**：`ocram_tdp` 有三条 generate 分支 `gInfer`/`gAltera`/`gSim`，为什么 `gSim` 要用 `SIMULATION` 守卫、且只编译 `ocram_tdp_sim.vhdl`？

> **答案**：仿真需要精确的读-写冲突行为与 X 传播处理（综合用的推断/原语模型受综合器限制，无法精确表达），所以仿真专用一个模型 `ocram_tdp_sim`。用 `SIMULATION` 守卫 + `.files` 里 `Environment = "Simulation"` 条件编译，能确保仿真模型不进入综合路径，避免「仿真与综合行为不一致」。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「按厂商跟踪 ocram 调用链」的综合任务。

**任务**：选定 `ocram_sp`，分别追踪它在 **Xilinx** 与 **Altera** 两个目标下的完整实现路径，并解释读-写冲突行为。

**步骤**：

1. **选型与接口**：从 [ocram.pkg.vhdl:41-55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl#L41-L55) 抄下 `ocram_sp` 的 generic/port，确认它满足「单端口、单时钟、写优先」需求。
2. **Xilinx 路径**：在 [ocram_sp.vhdl:90](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L90) 确认 `VENDOR_XILINX` 命中 `gInfer`；阅读 [L122-136](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L122-L136) 的进程，指出它用「寄存地址 + 组合读」得到写优先行为，综合器会推断成 BRAM。说明此时 `.files` 里**不会**编译 `ocram_sp_altera.vhdl`。
3. **Altera 路径**：在 [ocram_sp.vhdl:139](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L139) 确认 `VENDOR_ALTERA` 命中 `gAltera`；跟踪到 [ocram_sp_altera.vhdl:103-128](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/altera/ocram_sp_altera.vhdl#L103-L128)，指出它实例化 `altsyncram`（`operation_mode="SINGLE_PORT"`）。再查 [ocram_sp.files:15-18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.files#L15-L18) 确认此时会引入 `altera_mf` 库并先编译子实体。
4. **行为一致性**：对照 [ocram_sp.vhdl:31-35](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram_sp.vhdl#L31-L35) 的注释，说明两条路径对外都承诺「写优先」——这就是包装实体把厂商差异藏起来、对外提供一致行为的价值。

**交付物**：一张含两列（Xilinx / Altera）的对比表，每列写明「命中的 generate 分支」「底层实现（推断的 RTL 数组 vs `altsyncram` 原语）」「`.files` 是否引入 `altera_mf`」「读-写冲突行为」。

> 待本地验证：综合路径需在对应厂商工具链中验证；若本地无工具，至少完成源码阅读与调用链绘制。

---

## 6. 本讲小结

- ocram 是 PoC 对「片上 RAM」的统一抽象，按端口拓扑分成 `sp`/`sdp`/`esdp`(废弃)/`tdp` 以及 `_wf`（写优先）等配置，generic 统一为 `A_BITS`/`D_BITS`/`FILENAME`，深度 `DEPTH = 2**A_BITS`。
- 通用 ocram 实体用「数组 + 钟控进程」让综合器**推断** BRAM；读-写冲突行为由写法决定——「寄存地址 + 组合读」= 写优先（`sp`/`*_wf`），「读写分进程、寄存数据」= 无所谓（`sdp`/`tdp` 混口）。
- 厂商实例化沿用 [u3-l2](u3-l2-vendor-selection-portability.md) 的双层选择：`generate` 在 `gInfer`/`gAltera`/`gSim` 间展开期分发，`.files` 在编译期按 `DeviceVendor`/`Environment` 选文件；是否实例化原语取决于「该厂商能否正确推断该配置」。
- Altera 单端口走 `ocram_sp_altera` → 实例化 `altsyncram`（`altera_mf` 库，`SINGLE_PORT` 模式），因为 Quartus 推断不出；而 `ocram_sdp` 用 `ramstyle="no_rw_check"` 属性走推断。
- 仿真永远用专用模型 `ocram_tdp_sim`（精确模拟 RDW 与 X 传播），由 `SIMULATION` 守卫与 `.files` 的 `Environment` 条件隔离开综合路径。
- 未覆盖厂商由 `assert ... severity failure` 兜底，拒绝静默生成错误硬件。

---

## 7. 下一步学习建议

- **下一步学 [u3-l4 FIFO 家族](u3-l4-fifo-family.md)**：FIFO 是 ocram 最重要的消费者。读完本讲后，去看 `fifo_cc_got`/`fifo_ic_got` 是如何实例化 `ocram_sdp` 作为底层存储、并在其上叠加指针与填充指示器的，你会对本讲的「选型矩阵」有更深的体会。
- **回顾 [u3-l2 厂商选择](u3-l2-vendor-selection-portability.md)**：把 `sync_Bits` 与 `ocram_sp` 的 generate 分发对比着看，体会「双层选择」框架在不同资源（寄存器 vs 存储器）上的一致性。
- **进阶阅读源码**：`ocram_tdp_sim.vhdl`（仿真模型如何精确描述混口读-写冲突）、`ocram/pkg.vhdl` 之外的 `ocrom`（on-chip ROM）家族，以及 `cache` 命名空间（[u5-l3](u5-l3-cache-subsystem.md)）如何把 ocram 用作缓存数据阵列。
- **动手建议**：仿照本讲的对比表，自己为 `ocram_tdp` 也画一张 Xilinx/Altera/仿真三路调用链图，检验是否真正掌握了「推断 vs 原语 vs 仿真模型」三分支的取舍逻辑。
