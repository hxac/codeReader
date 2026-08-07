# RTL 顶层与四种封装

> 承接上一讲《仓库目录结构与顶层构建系统》。上一讲我们知道了 `rtl/` 目录里有四种「顶层封装」文件，并且 `make rtl` 会把它们连同 `zipcore` 一起编译成 Verilator 模型。本讲我们就打开这四个文件，看看它们的端口长什么样、彼此有什么区别、以及该按什么原则选用。

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `zipcore`（CPU 内核）与四种顶层封装（`zipsystem` / `zipbones` / `zipaxil` / `zipaxi`）之间的关系，特别是 Wishbone 封装与 AXI 封装在「如何包裹内核」上的不同路径。
- 读懂四个顶层模块的端口列表，区分它们各自提供哪些总线接口（Wishbone 单总线 vs. AXI 指令/数据分离总线 vs. AXI-Lite 调试口）。
- 说出 `zipsystem` 比 `zipbones` 多出来的接口与内部资源（外设、DMA、计数器、调试空间、外部中断位宽）。
- 根据项目的总线需求（Wishbone / AXI-Lite / AXI4、是否需要片内外设）选择合适的顶层封装。

## 2. 前置知识

在进入源码前，先建立几个直觉：

- **软核（soft core）**：CPU 是用 Verilog 描述的逻辑，可以放进 FPGA。它本身不包含内存、串口这些「外设」，需要一个**总线**把外设挂上去。
- **总线接口**：CPU 通过一组信号线和外界交换数据。ZipCPU 支持三种总线协议：
  - **Wishbone**：经典、简单的开源总线，一次握手（`cyc/stb/ack`）完成一次访问。
  - **AXI4-Lite**：Xilinx 生态常用的简化版 AXI，每次也是单笔传输，但用 AW/W/B/AR/R 五个通道拆开。
  - **AXI4**：完整版 AXI，支持**突发（burst）**——一次请求可以连续搬多个数据，带 ID、LEN、SIZE、BURST 等字段。
- **主（master）与从（slave）**：CPU 是「总线主设备」发起读写；外设/内存是「从设备」响应。调试口在 ZipCPU 里是一个「从设备」端口，让外部调试器能停下来读写 CPU 内部寄存器。
- **指令总线与数据总线**：取指和访存是两类不同的访问。可以共用一根总线（省端口），也可以分开两根（提高并发）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/zipbones.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v) | **精简 Wishbone 封装**：只有 CPU + 一条 Wishbone 出口，片内外设留给用户自己接。 |
| [rtl/zipsystem.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v) | **带外设的 Wishbone 封装**：在 `zipbones` 基础上集成了定时器、中断控制器、看门狗、DMA、性能计数器等片内外设。 |
| [rtl/zipaxil.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v) | **AXI4-Lite 封装**：指令、数据、调试三个 AXI-Lite 接口，指令与数据总线**分离**。 |
| [rtl/zipaxi.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v) | **AXI4 封装**：完整 AXI4，支持突发与缓存预取，性能最高、端口最多。 |
| [rtl/core/zipcore.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v) | 四个封装共同包裹的**CPU 内核**（本讲只看它被如何「装进」封装，不深入内核）。 |
| [rtl/core/zipwb.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v) | Wishbone 封装内部的「中间层」：实例化 `zipcore` + 取指/访存控制器，并把两者**仲裁合并成一条** Wishbone 出口。 |
| [README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md) | 明确说明了「Wishbone 封装共用总线、AXI 封装指令/数据分离」这一关键区别。 |

## 4. 核心概念与源码讲解

> 在逐个看封装前，先建立一个贯穿全讲的**核心关系图**：
>
> ```
>            ┌──────────────── 四种顶层封装 ────────────────┐
>            │                                              │
>   Wishbone │ zipbones ──┐                                 │
>   (共用总线)│            ├──> 都实例化 zipwb ──> zipcore   │
>            │ zipsystem ─┘   (合并取指+访存为一条 WB 口)    │
>            │                                              │
>   AXI      │ zipaxil ──┐                                  │
>   (I/D分离)│           ├──> 都直接实例化 zipcore          │
>            │ zipaxi ───┘   + 各自的 axi* 取指/访存模块     │
>            └──────────────────────────────────────────────┘
> ```
>
> 也就是说，规范里常说「四种封装包裹同一个 `zipcore`」是**最终结论**，但实现的**路径不同**：
> - 两个 **Wishbone 封装**通过中间模块 `zipwb` 间接包裹内核，`zipwb` 还负责把取指访问和访存访问**仲裁合并成一条 Wishbone 出口**（所以 Wishbone 封装只有一条对外的数据总线）；
> - 两个 **AXI 封装**直接实例化 `zipcore`，再配上各自的取指/访存模块（`axilfetch`/`axilops`/`axiicache`/`axidcache`/`axipipe` 等），因此指令总线和数据总线是**分离**的。
>
> 这一点 README 写得很清楚：Wishbone 封装「为指令和数据共用总线」，而 AXI4-Lite 和 AXI4 封装「为指令和数据各有一套独立总线接口」。见 [README.md:23-26](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L23-L26)。`zipwb` 的合并职责见 [rtl/core/README.md:67-70](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/README.md#L67-L70)。

下面四个最小模块分别对应四种封装。

### 4.1 zipbones：精简 Wishbone 封装

#### 4.1.1 概念说明

`zipbones` 的设计哲学写在了文件头注释里：「为了保持 ZipCPU 的小巧，这是一个**不带任何外设**的封装——你想要的任何外设都得在模块外部自己实现」。它是四个封装里最简单的，对外只暴露三样东西：

1. 一条 **Wishbone 主设备总线**（CPU 用它读写内存/外设）；
2. 一条 **Wishbone 从设备调试总线**（调试器用它停 CPU、读写寄存器）；
3. 一个**外部中断**输入 `i_ext_int` 和一个输出 `o_ext_int`。

#### 4.1.2 核心流程

`zipbones` 内部把绝大部分工作交给 `zipwb`：

```
i_clk/i_reset ──> (调试控制逻辑: halt/step/reset) ──> cpu_reset/cpu_halt
                                                              │
i_ext_int ──────────────────────────────────────────> zipwb ──> zipcore
i_dbg_*   ──> (调试寄存器读写解码) ────────────────────┘   │  (合并)
                                                            ▼
                              <────────────────────── 单条 Wishbone 出口 o_wb_*
```

注意它实例化 `zipwb` 时传了 `.WITH_LOCAL_BUS(0)`，意思是**不启用**「本地外设总线」——这正是它和 `zipsystem` 的根本区别。

#### 4.1.3 源码精读

**模块参数**（节选）：[rtl/zipbones.v:41-109](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L41-L109)

```verilog
module zipbones #(
    parameter RESET_ADDRESS=32'h1000_0000,   // 复位后 PC 起点
               ADDRESS_WIDTH=32,
    parameter BUS_WIDTH=32,                  // 总线数据宽度
    parameter [0:0] OPT_PIPELINED=1,
    parameter       OPT_LGICACHE = 2,        // 指令缓存大小(2^N)，这里偏小
    parameter       OPT_LGDCACHE = 0,        // 0 = 无数据缓存
    parameter       OPT_MPY = 3, OPT_DIV=1, OPT_SHIFTS=1, OPT_FPU=0,
    parameter [0:0] OPT_CIS=1, OPT_USERMODE=1,
    parameter [0:0] START_HALTED=1,          // 上电即停，等调试器
    parameter [0:0] OPT_DBGPORT=START_HALTED ...
```

这些 `OPT_*` 是「构建期裁剪开关」，决定 CPU 有没有乘法器、除法器、缓存、用户模式等。`zipbones` 默认 `OPT_LGDCACHE=0`（无数据缓存），整体偏精简。

**端口列表**（节选）：[rtl/zipbones.v:110-137](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L110-L137)

| 端口分组 | 信号 | 说明 |
|----------|------|------|
| 时钟/复位 | `i_clk, i_reset` | 单时钟、高有效复位 |
| Wishbone 主 | `o_wb_cyc/stb/we/addr/data/sel` | 发起访问（注意只有**一组**） |
| Wishbone 主返回 | `i_wb_stall/ack/data/err` | 从设备响应 |
| 中断 | `i_ext_int`（1 位）、`o_ext_int` | 单线中断进出 |
| 调试从 | `i_dbg_cyc/stb/we/addr[5:0]/data/sel`、`o_dbg_stall/ack/data` | 调试端口（注意 `i_dbg_addr` 是 **6 位**） |
| 杂项 | `o_cpu_debug`、`o_prof_*` | CPU 状态总线、性能采样 |

**关键实例化**：`zipbones` 用 `WITH_LOCAL_BUS(0)` 实例化 `zipwb`，再由 `zipwb` 实例化 `zipcore`。见 [rtl/zipbones.v:610-672](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L610-L672)，其中 `.WITH_LOCAL_BUS(0)` 在 [第 634 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L634)。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 `zipbones` 对外只有一条 Wishbone 出口。
2. **步骤**：打开 [rtl/zipbones.v:110-137](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L110-L137)，统计所有 `o_wb_*` / `i_wb_*` 信号；确认没有任何 `M_INSN_*` / `M_DATA_*` 之分。
3. **观察现象**：取指和访存合并到了同一组 `o_wb_*` 上。
4. **预期结果**：你能数出恰好一组 Wishbone 主端口（约 6 个输出 + 4 个输入），印证「Wishbone 封装共用总线」。
5. 若想进一步验证，可在 [rtl/zipwb.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v) 里找到把指令/数据两路仲裁成一路的逻辑——**待本地确认具体行号**。

#### 4.1.5 小练习与答案

**练习 1**：`zipbones` 默认 `START_HALTED=1`、`OPT_DBGPORT=START_HALTED`。这意味着什么？
**答**：CPU 上电后停在 halted 状态，必须先通过调试端口发命令才能开始跑程序；同时调试端口默认被启用。这适合调试期，正式部署时通常把 `START_HALTED` 设 0。

**练习 2**：为什么 `zipbones` 把 `i_wb_err` 与内部 `cpu_lcl_cyc`「或」后再喂给 `zipwb`（见 [zipbones.v:662](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L662)）？
**答**：因为 `zipbones` 关闭了本地总线（`WITH_LOCAL_BUS(0)`），任何「本地」访问都应被当作错误回报，所以用 `cpu_lcl_cyc`（CPU 试图访问本地空间）强制产生一个 bus error，防止无效访问被默默吞掉。

---

### 4.2 zipsystem：带外设的 Wishbone 封装

#### 4.2.1 概念说明

`zipsystem` 与 `zipbones` 共用同样的外部「骨架」（一条 Wishbone 主出口 + 一个调试从端口 + 中断），但它在**片内**额外集成了一整套外设。规范里这样描述它：「`ZipSystem` 封装包含一组可被 CPU 内部访问的最小外设集合」。也就是说，`zipsystem` 自带一个小型 SoC 的「片上外设」，而 `zipbones` 把这些统统留给你自己接。

#### 4.2.2 核心流程

`zipsystem` 的内部拓扑（高层）：

```
                 ┌─────────────── 内部 sys 总线(地址译码) ───────────────┐
   zipcore<--zipwb┤ PIC(中断控制器)│定时器A/B/C│Jiffies│看门狗│总线看门狗│
   (WITH_LOCAL   │ 性能计数器×8   │ DMA(zipdma)│ (可选)MMU                │
    _BUS=1)       └──────────────────────────────────────────────────────┘
                          ▲                                     │
   调试端口 dbg_* ──> 也能访问 sys 总线                          ▼
                          ┌──── wbpriarbiter(CPU vs DMA) ────> 单条 Wishbone 出口 o_wb_*
```

两个要点：
- CPU 的「本地访问」（高位地址命中片内外设）走 `sys` 总线；「全局访问」走外部 Wishbone。
- DMA 和 CPU 抢同一条对外出口，用一个**优先级仲裁器** `wbpriarbiter` 裁决。

#### 4.2.3 源码精读

**与 `zipbones` 相比，外部端口形状几乎一致，但有几处关键放大**：[rtl/zipsystem.v:203-337](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L203-L337)

| 端口 | zipbones | zipsystem | 原因 |
|------|----------|-----------|------|
| `i_ext_int` | **1 位** | **`EXTERNAL_INTERRUPTS` 位宽的向量** ([L217](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L217)) | 内置中断控制器要合并多路外部中断 |
| `i_dbg_addr` | **`[5:0]`（6 位）** | **`[6:0]`（7 位）** ([L223](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L223)) | 调试空间除了 CPU/命令，还要寻址 sys 外设总线 |
| Wishbone 主 / 调试从其余信号 | 同形 | 同形 | 对外协议不变 |

**多出来的参数**（节选）：[rtl/zipsystem.v:122-202](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L122-L202)

- `EXTERNAL_INTERRUPTS=1`（外部中断路数，[L143](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L143)）
- `OPT_LGICACHE=10`、`OPT_LGDCACHE=10`（默认**很大的缓存**，与 `zipbones` 的 2/0 形成对比，[L134/L139](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L134-L139)）
- `OPT_DMA=1`、`DMA_LGMEM=10`、`OPT_ACCOUNTING=1`（DMA 与性能计数，[L172-L177](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L172-L177)）
- `DELAY_DBG_BUS`、`DELAY_EXT_BUS`（给总线加一拍寄存器以助时序收敛，[L181-L185](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L181-L185)）

**片内外设实例**（这些 `zipbones` 完全没有）：
- 看门狗（由 `ziptimer` 实现，超时直接触发复位）：[zipsystem.v:960-974](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L960-L974)
- 总线看门狗 `wbwatchdog`（检测总线死锁）：[zipsystem.v:984-990](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L984-L990)
- 8 个性能/统计计数器 `zipcounter`（主/用户态各 4 个）：[zipsystem.v:1049-1140](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1049-L1140)
- DMA 控制器 `zipdma`：[zipsystem.v:1199-1225](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1199-L1225)
- 主/副中断控制器 `icontrol`：[zipsystem.v:1420-1451](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1420-L1451) 与 [L1276-L1304](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1276-L1304)
- 三个定时器 `ziptimer` + `zipjiffies`：[zipsystem.v:1355-1410](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1355-L1410)

**地址译码与仲裁**：
- `sys` 总线用高位地址选择外设：[zipsystem.v:658-665](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L658-L665)
- 片内外设基地址 `PERIPHBASE = 0xc0000000`（[L349](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L349)）；规范也说明「最高 8 位被置位的地址保留给本地外设，其它封装会直接转发给内存」，见 [spec.tex:1847-1851](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1847-L1851)。
- CPU 与 DMA 抢对外出口，用 `wbpriarbiter` 裁决：[zipsystem.v:1825-1840](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1825-L1840)
- 同样实例化 `zipwb`，但传 `.WITH_LOCAL_BUS(1'b1)`：[zipsystem.v:1529-L1567](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1529-L1567)

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：看懂调试端口如何「同时」访问 CPU 寄存器和片内外设。
2. **步骤**：阅读 [zipsystem.v:1683-1690](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1683-L1690)，看 `sys_cyc/sys_stb/sys_addr/sys_data` 是如何在「CPU 本地访问」与「调试器访问」之间二选一的。
3. **观察现象**：当 `cpu_lcl_cyc` 有效时，sys 总线归 CPU；否则若调试器地址命中 sys 区，调试器获得访问。
4. **预期结果**：CPU 优先；CPU 不占用时调试器才能读写外设。这正是注释里描述的仲裁规则。
5. 这部分行为依赖具体寄存器布局，**待本地结合仿真波形进一步确认**。

#### 4.2.5 小练习与答案

**练习 1**：`zipsystem` 的 `i_dbg_addr` 比 `zipbones` 多一位，多出的这一位用来干什么？
**答**：`zipbones` 的 6 位地址只够寻址「CPU 寄存器区」和「命令控制区」两类；`zipsystem` 多一位，用来再区分出「sys 外设区」，使调试端口能直接读写片上的定时器、PIC、DMA 等。

**练习 2**：为什么 `zipsystem` 默认 `OPT_LGDCACHE=10`，而 `zipbones` 默认 `0`？
**答**：`zipsystem` 定位为「能独立工作的完整系统」，默认给一个较大的数据缓存以提升性能；`zipbones` 定位为「最小内核」，默认不带数据缓存，由使用者按需打开。

---

### 4.3 zipaxil：AXI4-Lite 封装

#### 4.3.1 概念说明

`zipaxil` 把同一个 `zipcore` 暴露在 **AXI4-Lite** 协议下。和 Wishbone 封装最大的不同有两点：

1. **指令总线和数据总线分离**：有独立的 `M_INSN_*`（指令主）和 `M_DATA_*`（数据主）两套接口，取指和访存可以并发。
2. **调试口也是 AXI-Lite**：`S_DBG_*` 用标准的 AW/W/B/AR/R 五通道。

AXI-Lite 是 AXI4 的极简版：每次传输仍是「单笔」，没有 `LEN/SIZE/BURST/ID/LAST` 这些突发字段，接口简单、易于对接（例如 Xilinx 的 MIG、AXI 互联IP 都能直连）。

#### 4.3.2 核心流程

```
   S_AXI_ACLK/ARESETN (注意复位低有效 ARESETN)
        │
   S_DBG_* (AXI-Lite 从) ──> 调试控制 ──> zipcore
        │                                   │  取指                  访存
        │                                   ▼                        ▼
        │                        axilfetch ──> M_INSN_*      axilops/axilpipe ──> M_DATA_*
        │                        (可配 FETCH_LIMIT 单笔/批量)
        │
   i_interrupt ──────────────────────────────> zipcore
   o_cmd_reset/o_halted/o_gie/o_op_stall/o_pf_stall/o_i_count (性能观测)
```

注意：AXI 封装**直接实例化 `zipcore`**（不经过 `zipwb`），取指/访存分别用 `axilfetch`、`axilops`/`axilpipe` 等 AXI 版本模块。

#### 4.3.3 源码精读

**模块参数**（节选）：[rtl/zipaxil.v:51-87](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L51-L87)

```verilog
module zipaxil #(
    parameter C_DBG_ADDR_WIDTH = 8,
    parameter ADDRESS_WIDTH = 32,
    parameter C_AXI_DATA_WIDTH = 32,
    parameter OPT_LGICACHE = 0, OPT_LGDCACHE = 0,   // AXI-Lite 默认无缓存
    parameter [0:0] OPT_PIPELINED = 1'b1,
    parameter [0:0] START_HALTED = 1'b0,            // 注意:默认上电即跑
    parameter [0:0] SWAP_WSTRB = 1'b1,              // 字节使能字节序处理
    ...
```

**端口分组**：[rtl/zipaxil.v:88-294](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L88-L294)

| 接口 | 通道/信号 | 说明 |
|------|-----------|------|
| 时钟复位 | `S_AXI_ACLK, S_AXI_ARESETN, i_interrupt, i_cpu_reset` ([L90](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L90)) | AXI 风格低有效复位 |
| 调试从（AXI-Lite） | `S_DBG_AWVALID/READY/ADDR/PROT`、`S_DBG_W*`、`S_DBG_B*`、`S_DBG_AR*`、`S_DBG_R*` ([L105-L135](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L105-L135)) | 标准 5 通道，无 LEN/SIZE |
| 指令主（AXI-Lite） | `M_INSN_AW*/W*/B*/AR*/R*` ([L141-L164](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L141-L164)) | 与数据主**分离** |
| 数据主（AXI-Lite） | `M_DATA_AW*/W*/B*/AR*/R*` ([L168-L194](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L168-L194)) | 与指令主**分离** |
| 性能观测 | `o_cmd_reset, o_halted, o_gie, o_op_stall, o_pf_stall, o_i_count` ([L197-L202](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L197-L202)) | AXI 封装把统计信号引到顶层 |

**关键实例化**：直接实例化 `zipcore`（[zipaxil.v:837](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L837)），配合 AXI 版取指/访存模块 `axilfetch`（[L931](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L931)）、`axilpipe`（[L1019](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L1019)）、`axilops`（[L1082](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L1082)）。

另外有个本地参数控制每次预取多少笔：`FETCH_LIMIT = (OPT_LGICACHE < 4) ? (1<<OPT_LGICACHE) : 16`（[zipaxil.v:309](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L309)）——后续关于 AXI 取指的讲义会展开。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：确认 AXI-Lite 封装「没有」AXI4 的突发字段。
2. **步骤**：对比 [zipaxil.v:141-164](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L141-L164)（指令口）和下一节 [zipaxi.v:151-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L151-L192)。
3. **观察现象**：`zipaxil` 的 `M_INSN_*` 里只有 `AWVALID/READY/ADDR/PROT`、`WVALID/WDATA/WSTRB`、`BVALID/RESP`、`ARVALID/ADDR`、`RVALID/RDATA/RRESP`，没有 `LEN/SIZE/BURST/ID/WLAST/RLAST`。
4. **预期结果**：每笔传输都是单次，这正是 AXI-Lite 与 AXI4 的本质区别。
5. 这一对比是纯静态的端口阅读，可直接得出结论。

#### 4.3.5 小练习与答案

**练习 1**：`zipaxil` 默认 `START_HALTED=1'b0`，而两个 Wishbone 封装默认 `START_HALTED=1`。这意味着什么差异？
**答**：AXI 封装默认上电后**立即开始执行**程序（适合作为产品里的固化 CPU）；Wishbone 封装默认上电**停在 halted**，要靠调试器启动（适合开发/调试场景）。当然这只是默认值，都可配置。

**练习 2**：为什么 AXI 封装需要 `o_op_stall/o_pf_stall/o_i_count` 这些「性能观测」输出，而 Wishbone 封装没有把它们引到顶层？
**答**：Wishbone 的 `zipsystem` 把这些信号**内部**直接喂给了自带的 8 个 `zipcounter`；AXI 封装没有片上计数器，所以把这些原始统计信号引到顶层，供外部统计逻辑使用。

---

### 4.4 zipaxi：AXI4 封装

#### 4.4.1 概念说明

`zipaxi` 是四个封装里**功能最全、端口最多**的。它在 `zipaxil` 的基础上升级到**完整 AXI4**：支持突发传输（`LEN/SIZE/BURST`）、事务 ID（`AWID/ARID/BID/RID`）、末拍标志（`WLAST/RLAST`）以及缓存/锁/QoS 等属性位。配合指令缓存 `axiicache` 和数据缓存 `axidcache`，可以一次突发把整个缓存行读进来，吞吐最高。

和 `zipaxil` 一样，它**指令/数据总线分离**，且**直接实例化 `zipcore`**；调试口同样是 AXI-Lite（完整 AXI4 的主端口配上轻量的 AXI-Lite 调试从端口是常见做法）。

#### 4.4.2 核心流程

```
   S_AXI_ACLK/ARESETN
        │
   S_DBG_* (AXI-Lite 从) ──> 调试控制 ──> zipcore
                                       │  取指                        访存
                                       ▼                             ▼
                            axiicache/axilfetch ──> M_INSN_*   axidcache/axipipe/axiops ──> M_DATA_*
                            (突发读整条缓存行)        (AXI4, 带突发)
```

因为 AXI4 支持突发，指令缓存 `axiicache` 缺失时可以一次 `ARLEN=N` 的读事务拿回一整行指令，这是它比 `zipaxil` 性能高的根本原因。

#### 4.4.3 源码精读

**模块参数**（节选）：[rtl/zipaxi.v:51-97](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L51-L97)

```verilog
module zipaxi #(
    parameter C_AXI_ID_WIDTH = 1,        // AXI4 特有: 事务 ID 宽度
    parameter INSN_ID = 0, DATA_ID = 0,  // 指令/数据事务各自用的 ID
    parameter OPT_LGICACHE = 0, OPT_LGDCACHE = 0,
    parameter [0:0] OPT_WRAP = 1'b1,
    parameter LGILINESZ = 3, OPT_LGDLINESZ = 3,  // 缓存行大小(2^N 字)
    ...
```

`C_AXI_ID_WIDTH`、`INSN_ID`、`DATA_ID`、`LGILINESZ`、`OPT_LGDLINESZ` 这些参数在 `zipaxil` 里都不存在——它们都是为 AXI4 的突发与多事务并发而设。

**端口分组**（与 `zipaxil` 对照，多出的 AXI4 字段）：[rtl/zipaxi.v:98-256](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L98-L256)

| 接口 | AXI-Lite（zipaxil） | AXI4（zipaxi）多出的字段 |
|------|---------------------|--------------------------|
| 写地址 AW | `AWVALID/READY/ADDR/PROT` | `AWID, AWLEN, AWSIZE, AWBURST, AWLOCK, AWCACHE, AWQOS` |
| 写数据 W | `WVALID/WDATA/WSTRB` | `WLAST`（标识最后一拍） |
| 写响应 B | `BVALID/RESP` | `BID` |
| 读地址 AR | `ARVALID/ADDR/PROT` | `ARID, ARLEN, ARSIZE, ARBURST, ARLOCK, ARCACHE, ARQOS` |
| 读数据 R | `RVALID/RDATA/RESP` | `RID, RLAST` |

指令主示例见 [zipaxi.v:151-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L151-L192)，数据主见 [zipaxi.v:196-240](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L196-L240)。

**关键实例化**：直接实例化 `zipcore`（[zipaxi.v:790](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L790)），并挂上 AXI4 版的取指/访存/缓存模块：`axiicache`（[L888](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L888)）、`axilfetch`（[L944](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L944)）、`axidcache`（[L1060](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L1060)）、`axipipe`（[L1155](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L1155)）、`axiops`（[L1249](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L1249)）。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：体会 AXI4 突发字段与缓存行的关系。
2. **步骤**：在 [zipaxi.v:151-161](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L151-L161) 找到指令口的 `M_INSN_ARLEN`、`M_INSN_ARSIZE`、`M_INSN_ARBURST`；再结合参数 `LGILINESZ`（指令缓存行大小）思考一次缺失会发起多长的突发。
3. **观察现象**：这些字段在 `zipaxil` 里完全不存在。
4. **预期结果**：你能说清「AXI4 用一次 `ARLEN=行长-1` 的突发把整行指令读回缓存，而 AXI-Lite 只能一笔一笔读」。
5. 突发长度与缓存行的精确换算**待结合 `axiicache` 实现确认**，将在第 4 单元 AXI 讲义展开。

#### 4.4.5 小练习与答案

**练习 1**：`zipaxi` 的调试口 `S_DBG_*` 为什么仍然是 AXI-Lite，而不是完整 AXI4？
**答**：调试访问是低频、单笔的寄存器读写，用 AXI-Lite 足够且接线简单；只有需要高吞吐的指令/数据口才值得用完整 AXI4。这是工程上常见的「混合」做法。

**练习 2**：`zipaxi` 多了 `OPT_WRAP` 参数。它最可能控制什么？
**答**：从名字和 AXI4 语义推断，它控制是否使用 **WRAP（回绕）突发**类型——缓存行缺失时常用的突发模式（地址在行边界回绕）。具体行为**待确认** `axiicache`/`axipipe` 的实现。

---

## 5. 综合实践

> 本实践对应本讲任务卡：对比 `zipsystem` 与 `zipbones` 的端口，归纳各自适合的项目。

**任务**：对照阅读 [rtl/zipsystem.v:203-337](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L203-L337) 与 [rtl/zipbones.v:110-137](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L110-L137)，完成下表，并为每种封装写一句「适合什么项目」。

**参考答案（端口层面的差异）**：

| 对比项 | zipbones | zipsystem |
|--------|----------|-----------|
| 外部中断 `i_ext_int` | 单线（1 位） | 多线向量（`EXTERNAL_INTERRUPTS` 位） |
| 调试地址 `i_dbg_addr` | `[5:0]`（6 位） | `[6:0]`（7 位，多一位寻址片内外设区） |
| Wishbone 主出口 | 有，单条 | 有，单条（CPU 与 DMA 经 `wbpriarbiter` 共享） |
| 调试从端口 | 有 | 有（且能访问 sys 外设总线） |
| 默认指令/数据缓存 | `OPT_LGICACHE=2 / DCACHE=0`（小/无） | `ICACHE=10 / DCACHE=10`（大） |
| 片上外设 | **无**（外设全靠外部接） | 定时器×3、Jiffies、看门狗、总线看门狗、PIC×2、DMA、性能计数器×8 |
| 本地外设总线 | 关闭 `WITH_LOCAL_BUS=0` | 启用 `WITH_LOCAL_BUS=1` |
| 典型适用场景 | 你已有自己的外设/IP 库，只想塞一个最小 CPU；或追求面积最小 | 想要一个「开箱即用、自带常用外设」的完整小系统 |

**进阶（可选）**：把四者放一起，按「总线协议 / 是否分离 I&D / 是否带片内外设 / 端口规模」四个维度做一张总表，作为日后选型的速查表。一个粗略的选型口诀：

- **面积最小 / 已有外设库** → `zipbones`（Wishbone，单总线）。
- **Wishbone 生态、想要自带外设** → `zipsystem`。
- **Xilinx AXI-Lite 互联、单笔访问够用** → `zipaxil`。
- **高性能 AXI 互联、要突发和缓存** → `zipaxi`。

## 6. 本讲小结

- 四种顶层封装最终都包裹同一个 CPU 内核 `zipcore`，但路径不同：两个 Wishbone 封装经中间层 `zipwb`（合并取指+访存为一条出口），两个 AXI 封装直接实例化 `zipcore` 并配各自的取指/访存模块。
- **Wishbone 封装（`zipsystem`/`zipbones`）共用一条总线**；**AXI 封装（`zipaxil`/`zipaxi`）指令与数据总线分离**——这是 README 明示的根本区别。
- `zipbones` 是最精简的（无外设、无缓存），`zipsystem` 在同样的对外骨架上增加了定时器/PIC/看门狗/DMA/计数器等片内外设、更大的默认缓存，以及更宽的中断与调试地址。
- AXI-Lite 与 AXI4 的端口差异核心在于：AXI4 多出 `ID/LEN/SIZE/BURST/WLAST/RLAST` 等突发字段，从而支持缓存行突发预取，吞吐更高。
- 选择封装的依据：总线协议（Wishbone / AXI-Lite / AXI4）、是否需要分离的 I&D 总线、是否需要片内外设、以及面积/性能取舍。

## 7. 下一步学习建议

- 本讲只看了「外壳」。下一单元（第 2 单元）将进入 **ISA 规范**，搞清这个 CPU 到底能执行什么指令、有哪些寄存器与中断模型。
- 想先「跑起来」再回头读端口，可直接跳到 [u1-l4 跑起来：模拟器与第一个程序](u1-l4-first-simulation.md)，用 `sim/verilator` 的 `zipsys_tb`/`zipbones_tb` 把 `zipsystem`/`zipbones` 实际仿真一遍。
- 对封装内部「总线如何仲裁、外设如何挂接」感兴趣，可提前浏览第 4 单元的 `zipwb`、`wbpriarbiter`、`zipsystem` 地址译码相关讲义；对 AXI 取指/访存的 `FETCH_LIMIT`、突发缓存行等细节，留待第 4 单元「AXI 与 AXI-Lite 封装」展开。
