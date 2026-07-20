# 上下文存储模型：流上下文与窗口上下文

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么多流、多窗口的 DAQ IP 不能把所有配置和运行时状态都放进触发器，而必须用一块片上 RAM 来存——这块 RAM 就是「上下文存储（context memory）」。
- 画出**流上下文（Stream Context）**的 5 个 32 位字段（SCFG、BUFSTART、WINSIZE、PTR、WINEND）在 RAM 里的布局，并指出哪个字段属于「半静态配置」、哪个属于「动态运行时状态」。
- 画出**窗口上下文（Window Context）**每个「流 × 窗口」单元的 4 个字段（WINCNT、LAST、TSLO、TSHI）及其含义。
- 解释 4 块 `psi_common_tdp_ram`（`i_mem_ctx_lo/hi`、`i_mem_win_lo/hi`）如何用 A/B 双口让 AXI 侧（CPU）与状态机侧（ClkMem）共享同一块存储、互不踩踏。
- 手工算出「访问某个流的某个字段」时，**AXI 侧字节地址**与**RAM 内部 B 口地址**分别等于多少，并理解为什么这两个端口的行号天然一致。

本讲承接 [u2-l1](u2-l1-common-package.md)（上下文访问 record 与 Sel 常量）与 [u3-l2](u3-l2-sm-context-calcaccess.md)/[u3-l3](u3-l3-sm-window-switch-ringbuf.md)（状态机如何经 B 口读上下文、算地址、回写上下文），把视角从「状态机的读写时序」下沉到「它读写的那块物理 RAM 到底长什么样、地址怎么编」。

## 2. 前置知识

### 2.1 什么是「上下文」

在多窗口环形 DMA 的设计里，软件要为每一条流配置一批**半静态参数**：缓冲区起始地址、每个窗口的大小、是否当环形缓冲用、是否允许覆盖、配置多少个窗口、当前在第几个窗口……而硬件在采集过程中还会不断产生一批**动态运行时状态**：当前 DMA 写指针推进到哪了、某个窗口已经写了多少字节、这一窗是不是因为触发而结束的、触发发生时的时间戳是多少。

把这些参数和状态全部展开成触发器是不可行的：流数最多 32（`MaxStreams_c`）、每流窗口最多 32（`MaxWindows_c`），全展开就是上千组寄存器，面积与时序都吃不消。于是项目把它们集中存进一块**片上 RAM**——这就是「上下文存储」。所谓「上下文」，就是「让硬件知道这条流当前处于什么状态、下一步该往哪里写」所需的全部信息。

### 2.2 为什么需要「双口」

这块 RAM 有两个互不相关的「消费者」：

- **CPU 侧**：经 AXI Slave（`S_Axi_Aclk` 时钟域）配置半静态参数、读回运行时状态给软件（如读当前 PTR、读窗口字节数）。
- **状态机侧**：控制状态机 `psi_ms_daq_daq_sm` 在 `ClkMem` 时钟域里，一边采集一边读旧上下文、算下一个 DMA 地址、再回写新上下文。

两个消费者处于**不同时钟域**，又要**同时**读写同一块存储——这正是真双口 RAM（true dual-port RAM，`psi_common_tdp_ram`）的用武之地：A 口接 AXI 侧，B 口接状态机侧，两侧各自用自己的时钟独立寻址。

### 2.3 与前面讲义的衔接

- [u2-l1](u2-l1-common-package.md) 已经定义了状态机访问这块 RAM 时用的 record 类型 `ToCtxStr_t` / `ToCtxWin_t` / `FromCtx_t`，以及选择常量 `CtxStr_Sel_*` / `CtxWin_Sel_*`。本讲回答的是：**这些 record 背后对应的物理比特，在 RAM 里是怎么摆放的**。
- [u3-l2](u3-l2-sm-context-calcaccess.md) / [u3-l3](u3-l3-sm-window-switch-ringbuf.md) 讲了状态机读上下文、算地址、回写上下文的**时序**；本讲补上它读写的那块 RAM 的**结构与编址**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/psi_ms_daq_reg_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd) | AXI Slave 寄存器接口；**本讲主角**。上下文存储的 4 块 `tdp_ram`、深度常量、A/B 双口地址译码全部在这里例化与拼接。 |
| [hdl/psi_ms_daq_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd) | 公共类型包。定义上下文访问 record（`ToCtxStr_t`/`ToCtxWin_t`/`FromCtx_t`）、Sel 选择常量与 SCFG 内部位移常量。 |
| [driver/psi_ms_daq.h](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h) | C 驱动头文件。`PSI_MS_DAQ_CTX_*` / `PSI_MS_DAQ_WIN_*` 寄存器宏定义了软件侧看到的字节地址布局。 |
| [driver/psi_ms_daq.c](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c) | C 驱动实现。`strAddrOffs` 的计算决定了窗口上下文在 AXI 地址空间里流与流之间的步长。 |

## 4. 核心概念与源码讲解

### 4.1 流上下文存储模型：5 个字段的布局与两块 RAM

#### 4.1.1 概念说明

每条流都需要一组**流级**的配置与运行时状态。项目把它们整理成 5 个 32 位字段：

| 字段 | 含义 | 性质 |
|------|------|------|
| **SCFG**（Stream Config） | 打包了 RINGBUF（是否环形）、OVERWRITE（是否允许覆盖）、WINCNT（配置的窗口数）、WINCUR（当前窗口号）四个子字段 | 半静态配置 + 小量动态（WINCUR 会随窗口切换变化） |
| **BUFSTART** | 该流缓冲区在 DDR 中的起始字节地址 | 半静态配置（软件 `Str_Configure` 时写） |
| **WINSIZE** | 每个窗口的字节大小 | 半静态配置 |
| **PTR** | 当前 DMA 写指针（字节地址） | 动态运行时（状态机每笔 DMA 后回写） |
| **WINEND** | 当前窗口的结束字节地址（= `bufstart + winSize` 或回绕点） | 动态运行时（窗口切换/回绕时刷新） |

> 注意区分两个「WINCNT」：SCFG 里 `[20:16]` 的 **WINCNT 是「该流配置了多少个窗口」**（静态）；而后面窗口上下文里每个窗口的 **WINCNT 寄存器是「这个窗口已经录了多少字节」**（动态，bit 31 还是触发标志）。两者完全不同，只是名字撞了，务必分清。

由于 AXI 数据总线是 32 位、而逻辑上想以 64 位为单位组织，项目把每个 64 位「项（entry）」拆成**低 32 位（Lo）+ 高 32 位（Hi）**两块独立 RAM。流上下文里相邻的两个字段共用一个 64 位项：

| Sel | Lo（低 dword） | Hi（高 dword） |
|-----|----------------|----------------|
| `00`（ScfgBufstart） | SCFG | BUFSTART |
| `01`（WinsizePtr） | WINSIZE | PTR |
| `10`（Winend） | WINEND | （未用） |
| `11` | （未用） | （未用） |

所以每条流占 4 个 Sel 项 × 8 字节 = 32 字节 = 8 个 dword，地址步长是 `0x20`。

#### 4.1.2 核心流程

流上下文的编址与访问流程：

1. **A 口（AXI 侧）**：CPU 给出 AXI 字节地址 `AccAddr`。用高位 `AccAddr[15:12] = 0x1` 判定「这是流上下文区」（地址段 `0x1000–0x1FFF`）。
2. **行号译码**：A 口 RAM 行地址 = `AccAddr[CtxStrAddrHigh_c:3]`（即按 8 字节粒度取地址位，丢掉最低 3 位）。
3. **Lo/Hi 选择**：`AccAddr[2]` 决定写/读哪块 RAM——`0` 选 Lo（如 SCFG、WINSIZE、WINEND），`1` 选 Hi（如 BUFSTART、PTR）。
4. **B 口（状态机侧）**：状态机给出 `(Stream, Sel)`，B 口地址 = `Stream & Sel`（按位拼接），与 A 口指向**同一行**；再用 `WenLo`/`WdatLo` 或 `WenHi`/`WdatHi` 选 Lo/Hi RAM。

每个流上下文字段对应的「Sel + Lo/Hi」表见 4.1.1。

#### 4.1.3 源码精读

深度常量与地址高位定义（每块 32 位 RAM 的行数）：

[hdl/psi_ms_daq_reg_axi.vhd:129-130](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L129-L130) — `DepthCtxStr_c := Streams_g * 32 / 8` 即每流 32 字节 ÷ 8 = 4 行，乘以流数得到**每块** Lo/Hi RAM 的深度；`CtxStrAddrHigh_c := log2ceil(Streams_g*32) - 1` 是 A 口行地址的最高位编号。

A 口侧的地址译码、写使能与 Lo/Hi 选择：

[hdl/psi_ms_daq_reg_axi.vhd:498-501](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L498-L501) — `AddrCtxStr` 用 `AccAddr[15:12]=0x1` 判定流上下文区；`CtxStr_WeLo`/`WeHi` 在「整字写且 `AccAddr[2]` 为 0/1」时分别写 Lo/Hi RAM；`CtxStr_AddrB` 把状态机的 `(Stream, Sel)` 拼成 B 口地址。

Lo/Hi 两块 RAM 的例化（关键注释解释了为什么要拆）：

[hdl/psi_ms_daq_reg_axi.vhd:503-523](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L503-L523) — 注释「Memory is split organized as 64 bit memory for historical reasons (Tosca TMEM is 64-bit)」说明：逻辑上是 64 位项，因历史目标平台 Tosca 的片上存储原语（TMEM）原生 64 位；移植到当前 FPGA 时，为高效映射到 32 位宽双口 RAM，把 64 位拆成 Lo/Hi 两块共享同一地址的 32 位 RAM。`i_mem_ctx_lo` 的 A 口接 `S_Axi_Aclk`、B 口接 `ClkMem`，正是双时钟域双口。

Hi RAM 例化：

[hdl/psi_ms_daq_reg_axi.vhd:526-543](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L526-L543) — `i_mem_ctx_hi` 与 Lo 完全同构，只是 A 口写使能换成 `CtxStr_WeHi`、数据换成 `AccWrData` 接到 `CtxStr_Rdval[63:32]`，B 口写使能/数据换成 `CtxStr_Cmd.WenHi`/`WdatHi`。

AXI 读回时在 64 位结果里选 Lo/Hi dword：

[hdl/psi_ms_daq_reg_axi.vhd:279-285](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L279-L285) — 读 MUX 用 `AddrReg[2]` 在 `CtxStr_Rdval[31:0]`（Lo）与 `[63:32]`（Hi）之间选一个送回 AXI。

#### 4.1.4 代码实践

**目标**：在源码里把「一个流上下文字段」从 AXI 地址一直跟到 RAM 行号，验证 SCFG 与 BUFSTART 共享同一行、只是 Lo/Hi 不同。

**步骤**：

1. 打开 [hdl/psi_ms_daq_reg_axi.vhd:498-501](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L498-L501)，确认 `CtxStr_WeLo`/`WeHi` 的条件里 `AccAddr(2)` 决定 Lo/Hi。
2. 假设 `Streams_g = 8`，则 `DepthCtxStr_c = 8*4 = 32` 行，`CtxStrAddrHigh_c = log2ceil(8*32)-1 = 7`。
3. 取流 0 的 SCFG（AXI 地址 `0x1000`）与 BUFSTART（`0x1004`）：
   - `0x1000` → `AccAddr[2]=0` → Lo RAM；行号 = `0x000 >> 3 = 0`。
   - `0x1004` → `AccAddr[2]=1` → Hi RAM；行号 = `0x004 >> 3 = 0`（`0x004` 的 `[7:3]` 仍是 0）。
4. 两者行号都是 0，确实落在同一个 64 位项的 Lo 与 Hi。

**预期结果**：SCFG 与 BUFSTART 在 RAM 里是「同一行、不同块」，验证了 64 位项的拆分布局。本实践为源码阅读型，**待本地验证**（需在仿真器里观察 RAM 存储内容才能直接看到）。

#### 4.1.5 小练习与答案

**练习 1**：SCFG 的 WINCUR 子字段在 SCFG 的哪几位？状态机修改 WINCUR 时会影响哪块 RAM（Lo/Hi）？

**答案**：根据 [hdl/psi_ms_daq_pkg.vhd:79](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L79)，`CtxStr_Sft_SCFG_WINCUR_c = 24`，即 `[28:24]`（5 位，支持最多 32 窗口）。SCFG 是 Sel=`00` 的 Lo dword，所以影响 **Lo RAM**（`i_mem_ctx_lo`）。

**练习 2**：为什么 PTR 用 Hi dword、而 WINSIZE 用 Lo dword，二者能放在同一个 Sel 项里？

**答案**：因为状态机在一次上下文读取里用 `CtxStr_Sel_WinsizePtr_c` 同时把 WINSIZE 与 PTR 两个 32 位字段作为一个 64 位项读出（`FromCtx_t.RdatLo`/`RdatHi`），减少访问拍数；Lo/Hi 拆分正好对应这两个字段。

---

### 4.2 窗口上下文存储模型：每个「流 × 窗口」的运行时状态

#### 4.2.1 概念说明

流上下文是「每流一份」。窗口上下文则是**每个「流 × 窗口」一份**——它记录的是「这个具体的窗口里发生了什么」。一共有 4 个 32 位字段：

| 字段 | 含义 |
|------|------|
| **WINCNT** | bit `[30:0]` = 该窗口已录的有效**字节数**；bit `[31]` = ISTRIG（本窗口是否因触发而结束）。**WINCNT=0 表示该窗口空闲**，这是驱动「释放窗口」的约定（见 [u1-l4](u1-l4-driver-quickstart.md)） |
| **LAST** | 该窗口最后一个**样本**（不是字节）的地址，供驱动环形解包用 |
| **TSLO** | 触发时间戳的低 32 位 |
| **TSHI** | 触发时间戳的高 32 位 |

窗口上下文也按 64 位项组织，但每个「流 × 窗口」只有 2 个 Sel 项：

| Sel | Lo（低 dword） | Hi（高 dword） |
|-----|----------------|----------------|
| `0`（WincntWinlast） | WINCNT | LAST |
| `1`（WinTs） | TSLO | TSHI |

所以每个「流 × 窗口」占 2 项 × 8 字节 = 16 字节 = 4 dword，地址步长是 `0x10`。

#### 4.2.2 核心流程

窗口上下文的编址比流上下文多一维「窗口」：

1. **A 口（AXI 侧）**：用 `AccAddr[15:14] = "01"` 判定「这是窗口上下文区」（地址段 `0x4000–0x7FFF`）。
2. **地址布局**：流与流之间按 `strAddrOffs` 步长排布，窗口与窗口之间按 `0x10` 步长排布，字段在窗口内偏移 `0/4/8/C`。
3. **行号译码**：A 口行地址 = `AccAddr[CtxWinAddrHigh_c:3]`。
4. **Lo/Hi 选择**：`AccAddr[2]` 选 Lo/Hi RAM（WINCNT/TSLO 在 Lo，LAST/TSHI 在 Hi）。
5. **B 口（状态机侧）**：B 口地址 = `Stream & Window & Sel`（三维按位拼接），与 A 口指向同一行；`WenLo/WenHi` 选 Lo/Hi。

#### 4.2.3 源码精读

窗口上下文深度常量（每块 RAM 的行数 = 流数 × 窗口数 × 2）：

[hdl/psi_ms_daq_reg_axi.vhd:137-138](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L137-L138) — `DepthCtxWin_c := Streams_g * MaxWindows_g * 16 / 8 = Streams_g*MaxWindows_g*2`，即每个「流×窗口」16 字节 ÷ 8 = 2 行，再乘以流数与窗口数。

A 口地址译码与 B 口三维地址拼接：

[hdl/psi_ms_daq_reg_axi.vhd:547-550](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L547-L550) — `AddrCtxWin` 用 `AccAddr[15:14]="01"` 判定窗口区；`CtxWin_AddrB := Stream(log2ceil(Streams_g)) & Window(log2ceil(MaxWindows_g)) & Sel`，注意这里的 Window 字段宽是 `log2ceil(MaxWindows_g)`（向上取整到 2 的幂），这正是驱动里 `strAddrOffs` 要把窗口数向上取整的原因。

Lo/Hi 两块窗口 RAM：

[hdl/psi_ms_daq_reg_axi.vhd:555-572](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L555-L572)（Lo）与 [hdl/psi_ms_daq_reg_axi.vhd:575-592](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L575-L592)（Hi）— 与流上下文同构，只是深度换成 `DepthCtxWin_c`、地址高位换成 `CtxWinAddrHigh_c`、B 口地址换成 `CtxWin_AddrB`。

驱动如何计算流与流之间的步长 `strAddrOffs`（即 `WIN_*` 宏的 `so` 参数）：

[driver/psi_ms_daq.c:143](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L143) — `inst_p->strAddrOffs = Pow(2, Log2Ceil(maxWindows))*0x10`：把 `maxWindows` 向上取整到 2 的幂再乘 `0x10`。这与硬件 B 口 `Window` 字段宽 `log2ceil(MaxWindows_g)` 严格对齐——只有流步长也是「2 的幂个窗口 × 16 字节」，A 口的线性地址才能与 B 口的 `Stream & Window & Sel` 位拼接落在同一行（详见 4.4）。

#### 4.2.4 代码实践

**目标**：理解为什么窗口上下文的 B 口地址里 `Window` 字段要用 `log2ceil(MaxWindows_g)` 位（向上取整），而不是 `MaxWindows_g` 的精确位数。

**步骤**：

1. 读 [hdl/psi_ms_daq_reg_axi.vhd:550](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L550)，确认 `to_unsigned(CtxWin_Cmd.Window, log2ceil(MaxWindows_g))`。
2. 假设 `MaxWindows_g = 5`，则 `log2ceil(5) = 3`，Window 字段占 3 位（能表示 0..7），实际只用 0..4。
3. 读 [driver/psi_ms_daq.c:143](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L143)，`strAddrOffs = Pow(2, Log2Ceil(5))*0x10 = 8*0x10 = 0x80`，即每流预留 8 个窗口的空间（多预留 3 个不用）。
4. 思考：若改成精确的 `5*0x10 = 0x50` 步长会怎样？流 1 的窗口 0 会落在 `0x4000 + 0x50 = 0x4050`，其地址位无法与「Stream=1, Window=0」的位拼接对齐，A/B 两口行号就会错位。

**预期结果**：硬件必须把窗口维向上取整到 2 的幂，才能让 AXI 线性地址与 B 口位拼接地址天然一致；驱动的 `strAddrOffs` 是这套约定的软件镜像。**待本地验证**（可在仿真里改 `MaxWindows_g` 观察地址分布）。

#### 4.2.5 小练习与答案

**练习 1**：驱动把窗口「释放」给硬件时，写的是窗口上下文的哪个字段、写成什么值？

**答案**：写 WINCNT 字段为 0。因为 WINCNT 的 `[30:0]` 是字节数、`=0` 即「窗口空闲」，硬件据此判定该窗口可被 `winOverwrite=false` 路径重新写入（详见 [u4-l5](u4-l5-window-protection-overwrite.md)）。

**练习 2**：时间戳是 64 位的，为什么 TSLO 与 TSHI 能放在同一个 Sel 项里一次读出？

**答案**：TSLO 是 Sel=`1`（WinTs）的 Lo dword、TSHI 是其 Hi dword，构成一个 64 位项；状态机/驱动一次访问即可拿到完整 64 位时间戳（`RdatLo`/`RdatHi`），与触发时间戳的 64 位宽度天然匹配。

---

### 4.3 B 口地址拼接：`Stream & Sel` 与 `Stream & Window & Sel`

#### 4.3.1 概念说明

状态机经 B 口访问上下文时，不传「字节地址」，而是直接传逻辑坐标 `(Stream, Sel)` 或 `(Stream, Window, Sel)`，由 `reg_axi` 把它们**按位拼接**成 RAM 行地址：

- 流上下文 B 口地址 = `Stream & Sel`（Sel 2 位）
- 窗口上下文 B 口地址 = `Stream & Window & Sel`（Sel 1 位）

其中 `Stream` 占 `log2ceil(Streams_g)` 位、`Window` 占 `log2ceil(MaxWindows_g)` 位。这种「位拼接」编址的好处是：状态机用整数 `Stream`/`Window` 直接寻址，无需算乘法；代价是每一维必须向上取整到 2 的幂（所以驱动才有 `strAddrOffs` 那个取整）。

#### 4.3.2 核心流程

设 \(S\) 为 `Streams_g`、\(W\) 为 `MaxWindows_g`，记 \(s_b=\lceil\log_2 S\rceil\)、\(w_b=\lceil\log_2 W\rceil\)。

流上下文 B 口地址位宽：

\[
\text{CtxStr\_AddrB} = \text{Stream}(s_b\text{ 位}) \,\&\, \text{Sel}(2\text{ 位}) \quad\Rightarrow\quad s_b+2 \text{ 位}
\]

窗口上下文 B 口地址位宽：

\[
\text{CtxWin\_AddrB} = \text{Stream}(s_b\text{ 位}) \,\&\, \text{Window}(w_b\text{ 位}) \,\&\, \text{Sel}(1\text{ 位}) \quad\Rightarrow\quad s_b+w_b+1 \text{ 位}
\]

状态机用法：先置 `CtxStr_Cmd.Stream := r.HndlStream` 与 `CtxStr_Cmd.Sel := CtxStr_Sel_*`，再拉 `Rd`/`WenLo`/`WenHi`；`reg_axi` 自动拼出 B 口地址、读写对应 RAM 行。

#### 4.3.3 源码精读

流上下文 B 口地址拼接：

[hdl/psi_ms_daq_reg_axi.vhd:501](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L501) — `CtxStr_AddrB <= to_unsigned(CtxStr_Cmd.Stream, log2ceil(Streams_g)) & CtxStr_Cmd.Sel`。

窗口上下文 B 口地址拼接：

[hdl/psi_ms_daq_reg_axi.vhd:550](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L550) — `CtxWin_AddrB <= to_unsigned(CtxWin_Cmd.Stream, log2ceil(Streams_g)) & to_unsigned(CtxWin_Cmd.Window, log2ceil(MaxWindows_g)) & CtxWin_Cmd.Sel`。

状态机侧如何驱动这些字段（证明 B 口坐标来自状态机的整数寄存器）：

[hdl/psi_ms_daq_daq_sm.vhd:349-354](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L349-L354) — `v.CtxStr_Cmd.Stream := r.HndlStream`，并按拍令 `Sel` 在 `Winend` / `WinsizePtr` 等之间切换、拉 `Rd`。这正是 [u3-l2](u3-l2-sm-context-calcaccess.md) 讲的「多拍读取上下文」在 B 口坐标上的体现。

Sel 选择常量定义在公共包：

[hdl/psi_ms_daq_pkg.vhd:73-75](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L73-L75)（流上下文 Sel）与 [hdl/psi_ms_daq_pkg.vhd:91-92](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L91-L92)（窗口上下文 Sel）。

#### 4.3.4 代码实践

**目标**：验证「A 口行号」与「B 口地址」对同一逻辑项数值相等（这是双口共享存储能成立的关键）。

**步骤**：

1. 取流上下文流 3、Sel=`01`（WinsizePtr）：
   - B 口地址 = `to_unsigned(3, log2ceil(Streams_g)) & "01"`。若 `Streams_g=8`，则为 `"011" & "01" = "01101" = 13`。
2. 同一项的 AXI 地址（WINSIZE，Sel=`01` 的 Lo）= `0x1008 + 0x20*3 = 0x1068`：
   - A 口行号 = `0x1068[CtxStrAddrHigh_c:3]` = `(0x068)>>3` = `104>>3` = `13`。
3. 二者都等于 13。✓
4. 改试 PTR（Sel=`01` 的 Hi，AXI 地址 `0x106C`）：A 口行号 = `0x06C>>3 = 108>>3 = 13`，B 口地址仍是 13，只是 Lo/Hi 由 `AccAddr[2]` / `WenHi` 区分。

**预期结果**：A、B 两口对同一逻辑项落在同一行；Lo/Hi 由各自独立的信号控制。这正是真双口 RAM 能让两侧共享存储而不串位的根基。

#### 4.3.5 小练习与答案

**练习 1**：为什么 B 口地址用「位拼接」而不是「`base + offset` 加法」？

**答案**：位拼接无需加法器、时序与面积更省；状态机直接拿整数 `Stream`/`Window` 当高位坐标即可。代价是每维必须 2 的幂对齐（故有 `log2ceil` 与驱动 `strAddrOffs` 取整）。

**练习 2**：若把 `Streams_g` 从 8 改成 6，流 5 还能被正确寻址吗？

**答案**：能。`log2ceil(6) = 3`，Stream 字段 3 位可表示 0..7，覆盖流 0..5；只是流 6、7 的 RAM 空间被预留但不用（同窗口维向上取整的空耗）。

---

### 4.4 驱动寄存器宏与硬件地址译码的对应

#### 4.4.1 概念说明

软件看到的上下文是一组「字节地址 + 位字段」的寄存器宏，硬件看到的上下文是 4 块双口 RAM 的行/Lo/Hi。本模块把两侧对应起来：**驱动宏算出的 AXI 字节地址，经过 `reg_axi` 的高位译码与 `[7:3]` 行号、`[2]` Lo/Hi 提取，落到与 B 口完全相同的 RAM 行**。理解这层映射，是排查「软件读到/写错了上下文字段」类问题的钥匙。

#### 4.4.2 核心流程

软件写一个上下文字段的全链路：

1. 软件调用 `PsiMsDaq_RegWrite(baseAddr, PSI_MS_DAQ_CTX_PTR(n), value)`。
2. 宏 `PSI_MS_DAQ_CTX_PTR(n) = 0x100C + 0x20*n` 给出字节地址。
3. AXI Slave IPIF 把前 16 个寄存器（`0x00–0x3F`）截走做寄存器访问，其余路由到 memory 口；`reg_axi` 再 `+16*4` 把这 64 字节偏移加回来，使 `AccAddr` 复原成真实 AXI 字节地址。
4. `AccAddr[15:12]=0x1` 命中流上下文区；`AccAddr[2]` 选 Lo/Hi；`AccAddr[CtxStrAddrHigh_c:3]` 给出行号 → 写入对应 RAM 行。
5. 状态机随后用 `(Stream=n, Sel=01)` 经 B 口读到同一个 PTR。

#### 4.4.3 源码精读

驱动流上下文寄存器宏（含 SCFG 位字段）：

[driver/psi_ms_daq.h:164-174](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L164-L174) — `PSI_MS_DAQ_CTX_SCFG(n)=0x1000+0x20*n`、`BUFSTART=0x1004`、`WINSIZE=0x1008`、`PTR=0x100C`、`WINEND=0x1010`，流步长 `0x20`；SCFG 内 `RINGBUF`(bit0)、`OVERWRITE`(bit8)、`WINCNT`(`[20:16]`)、`WINCUR`(`[28:24]`) 与 [hdl/psi_ms_daq_pkg.vhd:76-79](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L76-L79) 的位移常量一一对应。

驱动窗口上下文寄存器宏：

[driver/psi_ms_daq.h:176-182](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L176-L182) — `PSI_MS_DAQ_WIN_WINCNT(n,w,so)=0x4000+so*n+0x10*w`、`LAST=0x4004`、`TSLO=0x4008`、`TSHI=0x400C`；WINCNT 的 `CNT` 在 `[30:0]`、`ISTRIG` 在 bit31。注意第三个参数 `so` 就是 [driver/psi_ms_daq.c:143](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L143) 算出的 `strAddrOffs`。

`AccAddr` 复原真实 AXI 地址（把 IPIF 截掉的寄存器空间加回来）：

[hdl/psi_ms_daq_reg_axi.vhd:436](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L436) — `AccAddr <= unsigned(AccAddrOffs) + 16*4`。这样后面 `AccAddr[15:12]=0x1`（流上下文）、`AccAddr[15:14]="01"`（窗口上下文）等高位译码才能直接对应驱动宏里的 `0x1000` / `0x4000` 基址。

读回路径在 64 位结果里选 dword：

[hdl/psi_ms_daq_reg_axi.vhd:286-292](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L286-L292) — 窗口上下文读回时用 `AddrReg[2]` 在 `CtxWin_Rdval[31:0]`（Lo）与 `[63:32]`（Hi）之间选择。

#### 4.4.4 代码实践

**目标**：把驱动读时间戳的调用链跟到 RAM，验证软件宏地址与硬件 B 口地址一致。

**步骤**：

1. 读 [driver/psi_ms_daq.c:572-573](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L572-L573)，确认 `PsiMsDaq_StrWin_GetTimestamp` 读 `PSI_MS_DAQ_WIN_TSLO` 与 `TSHI` 两个宏。
2. 取 `maxWindows=8`、流 2、窗口 5：`so = 8*0x10 = 0x80`，TSLO 地址 = `0x4008 + 0x80*2 + 0x10*5 = 0x4158`。
3. 硬件侧：`0x4158` 命中窗口区（`[15:14]="01"`），`[2]=0` 选 Lo RAM，行号 = `(0x158)>>3 = 344>>3 = 43`。
4. 状态机若经 B 口读同一时间戳：`Stream(2) & Window(5) & Sel(1) = "010" & "101" & "1" = "0101011" = 43`。两边都是 43、Lo RAM。✓

**预期结果**：软件读 TSLO 与硬件状态机读 TSLO 落在同一个物理 RAM 行，确认「软件地址 ↔ 硬件 B 口坐标」的映射自洽。

#### 4.4.5 小练习与答案

**练习 1**：软件配置一条流时写 SCFG、BUFSTART、WINSIZE 三个字段，硬件把它们分别写到哪块 RAM 的哪一侧？

**答案**：SCFG→Lo RAM（`AccAddr[2]=0`），BUFSTART→Hi RAM（`0x1004` 的 `[2]=1`），WINSIZE→Lo RAM（`0x1008` 的 `[2]=0`）。SCFG 与 WINSIZE 在不同 Sel 项（不同行），故虽同在 Lo RAM 但行号不同。

**练习 2**：为什么 `AccAddr` 要加 `16*4` 偏移？

**答案**：AXI Slave IPIF（`num_reg_g=16`）把前 16 个 dword（`0x00–0x3F`）留给寄存器访问，memory 口地址已扣掉这 64 字节；加回 `16*4=64` 才能让后续高位译码（`0x1000`/`0x4000`）与驱动宏基址吻合。

---

## 5. 综合实践

**任务**：给定实例化参数 `Streams_g = 8`、`MaxWindows_g = 8`（于是 \(s_b=w_b=3\)，`strAddrOffs = 0x80`），完成下面两套地址计算，并解释 64 位项被拆成高/低两块 32 位 RAM 的历史原因。

### 实践 A：访问流 3 的 PTR 字段

1. **AXI 侧字节地址**：`PSI_MS_DAQ_CTX_PTR(3) = 0x100C + 0x20*3 = 0x100C + 0x60 = 0x106C`。
2. **命中区**：`0x106C[15:12] = 0x1` → 流上下文区。
3. **Lo/Hi 与行号**：`0x106C[2] = 1` → **Hi RAM**（`i_mem_ctx_hi`）；行号 = `0x06C >> 3 = 108 >> 3 = 13`。
4. **RAM 内部 B 口地址**：PTR 属 Sel=`01`（WinsizePtr）的 Hi dword，故 `CtxStr_AddrB = Stream(3) & Sel(01) = "011" & "01" = "01101" = 13`。
5. **结论**：A 口行号 13、B 口地址 13，一致；落在 **`i_mem_ctx_hi`**（PTR 是高 dword）。

### 实践 B：访问流 2 窗口 5 的时间戳低 32 位（TSLO）

1. **AXI 侧字节地址**：`PSI_MS_DAQ_WIN_TSLO(2, 5, 0x80) = 0x4008 + 0x80*2 + 0x10*5 = 0x4008 + 0x100 + 0x50 = 0x4158`。
2. **命中区**：`0x4158[15:14] = "01"` → 窗口上下文区。
3. **Lo/Hi 与行号**：`0x4158[2] = 0` → **Lo RAM**（`i_mem_win_lo`）；行号 = `0x158 >> 3 = 344 >> 3 = 43`。
4. **RAM 内部 B 口地址**：TSLO 属 Sel=`1`（WinTs）的 Lo dword，故 `CtxWin_AddrB = Stream(2) & Window(5) & Sel(1) = "010" & "101" & "1" = "0101011" = 43`。
5. **结论**：A 口行号 43、B 口地址 43，一致；落在 **`i_mem_win_lo`**（TSLO 是低 dword）。

### 解释：为什么 64 位项要拆成高/低两块 32 位 RAM

源码注释 [hdl/psi_ms_daq_reg_axi.vhd:503](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L503) 与 [hdl/psi_ms_daq_reg_axi.vhd:552](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_reg_axi.vhd#L552) 写明：**"Memory is split organized as 64 bit memory for historical reasons (Tosca TMEM is 64-bit)"**。

- 逻辑上，上下文以 **64 位项**为单位组织（两个 32 位字段并排），这样状态机一次 B 口访问能同时拿到一对相关字段（如 WINSIZE+PTR、TSLO+TSHI），减少访问拍数。
- 这一布局最初是为 **Tosca 平台的 TMEM 原语**设计的——TMEM 原生 64 位宽，单个 64 位 RAM 即可完美承载。
- 移植到当前 FPGA（其片上 RAM 更适合用 32 位宽双口高效实现）时，**保留了 64 位项的逻辑布局**，但把每个 64 位项**拆成两块共享同一行地址的 32 位 RAM**：Lo RAM 存低 dword、Hi RAM 存高 dword。两侧（A 口 AXI、B 口状态机）各自用 `AccAddr[2]` / `WenHi` 决定操作哪一块。这就是「为历史原因而拆」的由来——编址方案与字段布局不变，只是物理上换成了两块 32 位 RAM。

**验收**：A、B 两题的 A 口行号与 B 口地址分别相等（13、43），且 PTR→Hi RAM、TSLO→Lo RAM 的判断正确，即说明你已掌握上下文存储的编址模型。

## 6. 本讲小结

- **上下文存储**是一块片上 RAM，集中保存多流多窗口的「半静态配置 + 动态运行时状态」，避免把它们全展开成触发器。
- 它分成两类：**流上下文**（每流 5 字段：SCFG/BUFSTART/WINSIZE/PTR/WINEND）与**窗口上下文**（每个「流×窗口」4 字段：WINCNT/LAST/TSLO/TSHI）。
- 物理上用 **4 块 `psi_common_tdp_ram`**：`i_mem_ctx_lo/hi`（流）、`i_mem_win_lo/hi`（窗口）；64 位项被拆成 Lo/Hi 两块 32 位 RAM，源于 Tosca TMEM 的 64 位历史布局。
- **A 口**接 AXI 侧（`S_Axi_Aclk`），用 `AccAddr` 高位译码区段、`[7:3]` 取行号、`[2]` 选 Lo/Hi；`AccAddr = AccAddrOffs + 16*4` 复原真实 AXI 地址。
- **B 口**接状态机侧（`ClkMem`），地址是位拼接：`CtxStr_AddrB = Stream & Sel`、`CtxWin_AddrB = Stream & Window & Sel`。
- 关键不变量：**对同一逻辑项，A 口行号 == B 口地址**——因为驱动的 AXI 地址布局（流步长 `strAddrOffs`、窗口步长 `0x10` 均按 2 的幂对齐）刻意镜像了 B 口的位拼接；这正是双口共享存储不串位的根基。

## 7. 下一步学习建议

- 阅读 [u3-l5 寄存器接口 psi_ms_daq_reg_axi：寄存器映射与 IRQ 聚合](u3-l5-register-interface.md)，把本讲的「上下文存储」与「真正的寄存器（GCFG/IRQVEC/STRENA/MAXLVL/POSTTRIG/MODE/LASTWIN）」合起来看完整的 AXI Slave 地址空间。
- 回看 [u3-l2](u3-l2-sm-context-calcaccess.md)/[u3-l3](u3-l3-sm-window-switch-ringbuf.md)，带着本讲的「行号 = Stream & Sel」的物理认知，重新理解状态机多拍读上下文与回写上下文的时序。
- 进阶阅读 [u4-l3 驱动数据读取与环形缓冲解包](u4-l3-driver-data-unwrap.md)，看软件如何用本讲的 LAST/WINCNT/TSLO 字段把环形窗口里的数据解包出来。
- 想验证地址映射，可在 `tb/psi_ms_daq_axi/psi_ms_daq_axi_tb_pkg.vhd` 的 AXI 寄存器驱动与共享内存模型里跟踪一次 `PSI_MS_DAQ_CTX_PTR` / `PSI_MS_DAQ_WIN_TSLO` 的写入，对照 RAM 内容确认行号与 Lo/Hi。
