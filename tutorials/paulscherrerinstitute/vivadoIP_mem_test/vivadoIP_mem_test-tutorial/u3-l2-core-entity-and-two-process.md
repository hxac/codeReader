# 核心实体接口与两进程设计

## 1. 本讲目标

u3-l1 把我们带到了核心实例 `i_logic` 的门口——我们知道它在三实例架构里是「大脑」，却还没打开它的盖子。本讲就钻进 `mem_test.vhd` 内部，建立两件事：**这个实体的对外契约长什么样**，以及**它的内部时序逻辑用什么编码风格写出来**。学完后你应当能够：

- 说出 `mem_test` 实体的两个 generic（`AxiAddrWidth_g`/`AxiDataWidth_g`）的取值范围、默认值与作用，并解释这个范围如何约束 IP 打包界面的合法参数。
- 逐组讲清 `CmdWr_*`/`CmdRd_*`/`WrDat_*`/`RdDat_*` 端口里每一个信号的方向与 valid/ready 握手含义，分清「命令」「数据」「响应」三类接口。
- 解释什么叫 **two-process（组合 + 寄存）设计法**：为什么所有寄存器被打包进一个记录 `two_process_r`，为什么用 `r`/`r_next` 两个信号，以及 `p_comb`/`p_reg` 各自的分工。
- 看懂 `p_comb` 里 `v := r` 这个赋值的作用——它如何让你「只写变化量」而不必每个分支都重述全部字段。
- 说出 `p_reg` 复位时**只清零 valid/ready 一类握手信号、不全清**的原因，并能据此判断哪些字段必须复位、哪些可以省。

本讲**只看骨架**：entity、记录、两进程的壳。状态机 `Fsm_t` 的具体流转（Idle→WrCmd→Write→…）留给 u3-l3，pattern 的四种生成算法留给 u3-l4。

## 2. 前置知识

继续用 u3-l1 的「工厂」比喻，再加几块新积木。

- **实体（entity）与架构（architecture）**：VHDL 里一个模块分两半。`entity` 是「外壳」，声明这个模块对外长什么样——有哪些 generic（配置参数）、哪些端口（输入输出信号）；`architecture` 是「内瓤」，描述这些端口之间如何互动。本讲先吃透外壳，再看内瓤的骨架。
- **generic 的范围约束**：VHDL 允许给 generic 加 `range`，例如 `natural range 12 to 64`。它不仅给读者看，综合器与 Vivado IP 打包器都会据此校验——你若把地址宽度配成 8，会在打包阶段就被拒绝。这把「合法配置」钉死在源码里。
- **valid/ready 握手**：AXI 与大多数现代流式接口都用这对信号传输一拍数据。发送方把数据放好、拉高 `Vld`（valid，有效）；接收方准备好时拉高 `Rdy`（ready，就绪）。**只有当 `Vld='1'` 且 `Rdy='1'` 在同一拍同时成立，这一拍才算传送成功**（一次「握手」）。本讲的命令/数据端口全都是这个套路。
- **记录类型（record）**：VHDL 的「结构体」——把若干个相关信号捆成一个整体，用 `.` 访问成员。例如 `r.Fsm`、`r.Errors`。把一堆寄存器塞进一个 record，能让你整组传递、整组复位，代码更紧凑。
- **组合逻辑 vs 时序逻辑**：组合逻辑的输出「即时」跟随输入变（只要输入变，输出立刻变，与时钟无关）；时序逻辑的输出只在时钟上升沿更新，能「记住」上一拍的状态（寄存器）。本讲的两进程法，本质就是**用一个组合进程算「下一拍该是什么」，再用一个时序进程把它存进寄存器**。
- **two-process 法的直觉**：想象你在写一份「明日计划」。你看着「今日状态」(`r`)，结合「今天发生的事」(输入)，写出「明天要变成什么样」(`r_next`)；到第二天零点（时钟上升沿），把 `r_next` 抄进 `r`，新的一天开始。`p_comb` 就是写计划，`p_reg` 就是零点抄写。

回顾 u2-l1 的寄存器接口模型（`rd_t/wr_t/rdata_t/wdata_t`，按寄存器编号访问）和 u3-l1 的三实例连线——本讲正是站在 `i_logic` 实例这一侧，看它「看见了什么端口、内部用什么风格实现」。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，另一个文件只借它的子类型定义。

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) | 核心测试逻辑（本讲主角） | entity 的 generic/端口、`two_process_r` 记录、`p_comb`/`p_reg` 两进程、并发赋值 |
| [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd) | 全局 package | 寄存器接口子类型 `rd_t/rdata_t/wr_t/wdata_t` 的定义 |

> 提示：状态机分支体、`InitPattern/UpdatePattern`、错误统计在本讲只点到为止，细节见 u3-l3、u3-l4。

## 4. 核心概念与源码讲解

### 4.1 entity 端口与 generics

#### 4.1.1 概念说明

`mem_test` 实体是核心逻辑的对外契约。它的端口分三大组：

1. **控制信号**：`Clk`（时钟）与 `Rst_n`（低有效复位）——所有时序逻辑的基石。
2. **寄存器接口**：`Reg_Rd/Reg_RData/Reg_Wr/Reg_WData` 四组，类型来自 package（u2-l1 已讲）。这是「办公室 ↔ 大脑」的电话线：CPU 经它下达配置、读回状态。
3. **AXI 主机用户接口**：`CmdWr_*/CmdRd_*/WrDat_*/RdDat_*` 加上 `Wr_Done/Wr_Error/Rd_Done/Rd_Error`。这是「大脑 ↔ 车间」的传送带：核心经它向主机下达写/读命令、喂/收数据、收完成与错误反馈。

generic 只有两个：`AxiAddrWidth_g`（地址位宽）与 `AxiDataWidth_g`（数据位宽）。它们不配 burst/outstanding——因为那些是主机 `i_master` 的实现细节，核心只发「写 N 字节」的高层命令（u3-l1 已强调）。

#### 4.1.2 核心流程

一次写命令在端口层面的握手时序（概念）：

```
          ┌── CmdWr_Addr/Size 给出命令参数
CmdWr_Vld ┤─────────────┐
          │             │
CmdWr_Rdy ────────┐     │
                  ▼     ▼
              握手成功的那一拍 → 命令被主机接收
```

- 核心把地址/大小放好，拉高 `CmdWr_Vld`。
- 主机准备好后拉高 `CmdWr_Rdy`。
- 两者同拍为 1，命令成立；下一拍核心可把 `CmdWr_Vld` 拉低。
- 随后核心在 `WrDat_Data/WrDat_Vld` 上逐拍送 pattern 数据，主机用 `WrDat_Rdy` 逐拍握手。
- 全部送完后，主机在 `Wr_Done` 上回一拍脉冲（出错则给 `Wr_Error`）。

读路径对称：核心发 `CmdRd_*`，主机回送 `RdDat_Data/RdDat_Vld`、核心用 `RdDat_Rdy` 握手，结束时给 `Rd_Done/Rd_Error`。

#### 4.1.3 源码精读

**generic 声明与取值范围**：

[hdl/mem_test.vhd:25-28](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L25-L28) —— `AxiAddrWidth_g : natural range 12 to 64 := 32`、`AxiDataWidth_g : natural range 16 to 1024 := 32`。注意三件事：(1) `range` 把合法区间钉死，综合/打包会校验；(2) 默认值都是 32；(3) 行尾 `-- $$ constant=32 $$` 是 PsiSim 框架的元数据注释，告诉仿真器「仿真时用常量 32 实例化，不必遍历所有取值组合」。

> 旁注：实体上方还有三条 `-- $$ ... $$` 注释（[hdl/mem_test.vhd:21-23](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L21-L23)），声明了 testbench 契约——支持的测试用例名（`simple_tf` 等）、TB 里将存在的进程（`user_cmd/user_data/user_resp/axi`）、TB 依赖的 package。它们对 VHDL 是普通注释，由 psi_tb/PsiSim 框架解析，用于文档化与 TB 脚手架生成。

**控制信号**：

[hdl/mem_test.vhd:31-32](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L31-L32) —— `Clk : in std_logic`、`Rst_n : in std_logic`。注释 `-- $$ type=clk; freq=100e6 $$` 标注这是 100 MHz 时钟；`-- $$ type=rst; clk=Clk; lowactive=true $$` 标注这是相对 `Clk` 的低有效复位。同样是给仿真框架看的元数据。

**寄存器接口端口**：

[hdl/mem_test.vhd:34-38](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L34-L38) —— `Reg_Rd : in rd_t`（读脉冲，输入）、`Reg_RData : out rdata_t`（回读数据，输出）、`Reg_Wr : in wr_t`（写脉冲）、`Reg_WData : in wdata_t`（写数据）。方向说明：**写相关（`Reg_Wr/Reg_WData`）与读脉冲（`Reg_Rd`）是从机送进来的输入；只有回读数据 `Reg_RData` 由核心驱动出去**——与 u3-l1 的信号驱动表一致。

类型定义在 package 中：

[hdl/mem_test_pkg.vhd:24-27](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L24-L27) —— `rd_t/wr_t` 是 32 位位向量（每位对应一个寄存器），`rdata_t/wdata_t` 是 32 元素的 `t_aslv32` 数组（每元素 32 位）。

**AXI 主机用户接口端口**，按功能分组阅读：

[hdl/mem_test.vhd:41-45](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L41-L45) —— **写命令** `CmdWr_Addr/CmdWr_Size`（地址、字节数，输出）、`CmdWr_LowLat`（低延迟标志，输出）、`CmdWr_Vld`（有效，输出）、`CmdWr_Rdy`（就绪，输入）。

[hdl/mem_test.vhd:46-50](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L46-L50) —— **读命令** `CmdRd_Addr/CmdRd_Size/CmdRd_LowLat/CmdRd_Vld`（输出）、`CmdRd_Rdy`（输入）。结构与写命令完全对称。

[hdl/mem_test.vhd:51-54](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L51-L54) —— **写数据** `WrDat_Data`（数据，输出，宽 `AxiDataWidth_g`）、`WrDat_Be`（字节使能，输出，宽 `AxiDataWidth_g/8`）、`WrDat_Vld`（输出）、`WrDat_Rdy`（输入）。

[hdl/mem_test.vhd:55-57](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L55-L57) —— **读数据** `RdDat_Data`（数据，输入）、`RdDat_Vld`（输入）、`RdDat_Rdy`（就绪，输出）。注意读数据方向翻转：数据与有效由主机送进来，就绪由核心送出去。

[hdl/mem_test.vhd:58-61](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L58-L61) —— **响应** `Wr_Done/Wr_Error/Rd_Done/Rd_Error`，全部是输入——完成与错误都由主机回报给核心。

端口方向速记表：

| 端口组 | 关键信号 | 方向（相对核心） | 握手对 |
| --- | --- | --- | --- |
| 写命令 | `CmdWr_Addr/Size/Vld` | 输出 | `CmdWr_Vld` ↔ `CmdWr_Rdy` |
| 写数据 | `WrDat_Data/Vld` | 输出 | `WrDat_Vld` ↔ `WrDat_Rdy` |
| 读命令 | `CmdRd_Addr/Size/Vld` | 输出 | `CmdRd_Vld` ↔ `CmdRd_Rdy` |
| 读数据 | `RdDat_Data/Vld` | 输入 | `RdDat_Vld` ↔ `RdDat_Rdy` |
| 响应 | `Wr_Done/Wr_Error/Rd_Done/Rd_Error` | 输入 | 单拍脉冲，无握手 |

#### 4.1.4 代码实践

**实践目标**：用源码确认「每个端口由谁驱动、走的是哪种握手」，巩固方向感。

**操作步骤（源码阅读型）**：

1. 打开 [hdl/mem_test.vhd:40-62](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L40-L62)。
2. 对每个端口，读它的 `in`/`out` 关键字，填一张「信号 → 方向 → 属于哪类握手对」的小表。
3. 验证：所有 `*_Vld` 中，写侧的 `CmdWr_Vld/WrDat_Vld/CmdRd_Vld` 是 `out`，读侧的 `RdDat_Vld` 是 `in`——因为写时核心是数据源、读时核心是数据宿。
4. 找出唯一一个由核心驱动的 `Rdy`：`RdDat_Rdy`（输出）。其余 `Rdy`（`CmdWr_Rdy/WrDat_Rdy/CmdRd_Rdy`）都是输入。

**需要观察的现象**：valid/ready 总是成对出现，且「谁当源、谁当宿」决定了 Vld 与 Rdy 各落在哪一侧。

**预期结果**：与上方「端口方向速记表」一致。本实践为纯源码阅读，结论可直接得出；如要核对综合后的端口方向，可在 Vivado 里看 `mem_test` 的端口列表，结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WrDat_Be`（字节使能）的位宽是 `AxiDataWidth_g/8`，而 `WrDat_Data` 是 `AxiDataWidth_g`？

**答案**：数据每 8 位（1 字节）配 1 位使能，故 `AxiDataWidth_g` 位的数据对应 `AxiDataWidth_g/8` 字节、即 `AxiDataWidth_g/8` 位使能。

**练习 2**：`CmdWr_LowLat` 是输出，但核心「并不真的需要它」——你怎么从本讲源码印证这一点？

**答案**：见 4.3.3 的并发赋值 `CmdWr_LowLat <= '0';`——核心把它恒接 `0`，从不动态改变。它是主机接口的一个可选优化标志，核心选择不用，固定拉低。

**练习 3**：若把 `AxiAddrWidth_g` 配成 8，会发生什么？

**答案**：源码 `range 12 to 64` 会在分析阶段直接报错——8 不在合法区间。这把「地址宽度至少 12」的设计约束硬编码进了契约。

---

### 4.2 two_process_r 记录

#### 4.2.1 概念说明

`mem_test` 的内部状态不少：当前状态机处在哪个状态、已经数到第几个错误、首个错误地址是多少、当前 pattern 值、向主机输出的命令地址/大小/有效……如果把这些散写成几十个独立 `signal`，进程的敏感表会又长又脆，复位语句也会铺满一屏。

本实体的解法是**把它们统统打包进一个记录 `two_process_r`**，再用这个记录类型声明两个信号：`r`（current，当前拍的真实状态）与 `r_next`（next，下一拍要变成的状态）。这就是 two-process 法的载体。

**为什么是两个信号而不是一个？** 因为时序逻辑有「读当前态、算下一态」的天然两段性：

\[ r_{\text{next}} = f\bigl(r,\ \text{inputs}\bigr) \]

即「下一拍的状态 `r_next`，由当前拍的状态 `r` 与当前输入共同决定」。组合进程算出这个 `f`，寄存进程在每个上升沿把 `r_next` 抄进 `r`。两信号分工明确、互不干扰。

#### 4.2.2 核心流程

记录在两进程之间的流动：

```
   ┌─────────────────┐                        ┌──────────────────┐
   │  r (当前状态)    │── 读 ─────────────────▶│   p_comb         │
   │                 │                        │  (组合：算下一态) │
   │                 │◀── 写(上升沿) ──────────│  写出 r_next     │
   └─────────────────┘                        └──────────────────┘
            │                                          ▲
            │ 读                                       │ 读 inputs
            ▼                                          │
   ┌─────────────────┐                        ┌──────────────────┐
   │  并发赋值        │                        │  p_reg          │
   │ (输出端口 = r.x) │                        │ (寄存：r<=r_next)│
   └─────────────────┘                        └──────────────────┘
```

要点：`p_comb` 同时读 `r` 与输入，算出 `r_next`；`p_reg` 只在时钟沿把 `r_next` 存进 `r`；所有对外输出端口都从 `r`（已寄存的当前态）经并发赋值引出——因此输出是**寄存后**的，时序干净。

#### 4.2.3 源码精读

**记录定义**：

[hdl/mem_test.vhd:94-111](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L94-L111) —— `two_process_r` 记录，16 个字段。注意位宽依赖 generic：`FirstErrAddr/CmdWr_Addr/CmdWr_Size/PatternCnt/CmdRd_Addr/CmdRd_Size` 都是 `AxiAddrWidth_g-1 downto 0`，`Pattern` 是 `AxiDataWidth_g-1 downto 0`——generic 一改，记录里多个字段位宽同步变。

**双信号声明**：

[hdl/mem_test.vhd:112](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L112) —— `signal r, r_next : two_process_r;`。一个当前态、一个下一态。

**字段分类表**（理解记录的关键——这也是本讲实践任务的一部分）：

| 字段 | 类型 | 分类 | 作用 |
| --- | --- | --- | --- |
| `Fsm` | `Fsm_t` | 状态机 | 当前所处状态（Idle/WrCmd/Write/RdCmd/Read/AxiError/IntError）|
| `Errors` | `unsigned(31 downto 0)` | 错误统计 | 累计的错误数据拍数 |
| `FirstErrAddr` | `unsigned(AxiAddrWidth_g-1 downto 0)` | 错误统计 | 第一个出错数据的地址 |
| `FirstErrFound` | `std_logic` | 错误统计 | 是否已记录过首个错误（去重标志）|
| `Pattern` | `slv(AxiDataWidth_g-1 downto 0)` | pattern/输出 | 当前 pattern 值；**兼作写数据输出**（见 4.3.3）|
| `PatternCnt` | `unsigned(AxiAddrWidth_g-1 downto 0)` | 进度计数 | 本轮已写/读的第几拍 |
| `CmdWr_Addr` | `unsigned(AxiAddrWidth_g-1 downto 0)` | 写命令输出 | 写命令地址 |
| `CmdWr_Size` | `unsigned(AxiAddrWidth_g-1 downto 0)` | 写命令输出 | 写命令大小（已折算成 beat）|
| `CmdWr_Vld` | `std_logic` | 写命令握手 | 写命令有效 |
| `WrDat_Vld` | `std_logic` | 写数据握手 | 写数据有效 |
| `CmdRd_Addr` | `unsigned(AxiAddrWidth_g-1 downto 0)` | 读命令输出 | 读命令地址 |
| `CmdRd_Size` | `unsigned(AxiAddrWidth_g-1 downto 0)` | 读命令输出 | 读命令大小 |
| `CmdRd_Vld` | `std_logic` | 读命令握手 | 读命令有效 |
| `RdDat_Rdy` | `std_logic` | 读数据握手 | 读数据就绪 |
| `ContRunning` | `std_logic` | continuous 模式 | 是否处于持续运行标志 |
| `ContIter` | `unsigned(31 downto 0)` | continuous 模式 | 已完成的迭代轮数 |

归纳成五大类：**① 状态机**（`Fsm`）；**② 错误统计**（`Errors/FirstErrAddr/FirstErrFound`）；**③ pattern 与进度**（`Pattern/PatternCnt`）；**④ 命令/数据输出与握手**（`CmdWr_*/WrDat_Vld/CmdRd_*/RdDat_Rdy`）；**⑤ continuous 模式跟踪**（`ContRunning/ContIter`）。

> 小巧思：注意记录里**没有** `WrDat_Data` 字段。因为写数据就是当前 pattern 值，源码直接用并发赋值 `WrDat_Data <= std_logic_vector(r.Pattern)`（4.3.3 详解），让 `Pattern` 一个字段身兼两职——既跟踪 pattern 演化、又充当写数据源。

**配套的枚举类型**（记录里 `Fsm` 的类型）：

[hdl/mem_test.vhd:72](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L72) —— `type Fsm_t is (Idle_s, WrCmd_s, Write_s, RdCmd_s, Read_s, AxiError_s, IntError_s);`。`Fsm` 字段就取这 7 个枚举值之一。本讲只用知道有这 7 个状态，流转细节见 u3-l3。

#### 4.2.4 代码实践

**实践目标**：把记录 16 个字段逐一分类，建立「这个字段为什么存在、属于哪一类」的心智模型（本讲指定实践任务的上半部分）。

**操作步骤（源码阅读型）**：

1. 对照 [hdl/mem_test.vhd:94-111](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L94-L111) 逐个字段，把它归入上文五大类之一。
2. 对每个字段自问：它**会被谁读**、**会被谁写**？例如 `Errors` 在 `Read_s` 里被加 1（写）、在 `Reg_RData(REG_ERRORS)` 处被回读（读）。
3. 找出「既是内部状态、又直接驱动输出端口」的字段——它们最值得注意，因为状态与输出耦合在一起。预期会发现 `Pattern`、`CmdWr_Vld`、`CmdRd_Vld`、`WrDat_Vld`、`RdDat_Rdy` 等。

**需要观察的现象**：握手类字段（各种 `*_Vld`、`RdDat_Rdy`）既是内部状态、又是对外端口，所以必须精心复位（见 4.3）；纯统计/进度字段（`Errors/PatternCnt/...`）只在内部流转。

**预期结果**：得到一张与「字段分类表」一致的归类，并确认 `Pattern` 一字段两用。本实践为源码阅读，结论确定。

#### 4.2.5 小练习与答案

**练习 1**：记录里为什么有 `CmdWr_Vld`，却没有 `CmdWr_Rdy`？

**答案**：`CmdWr_Vld` 由核心驱动，属于核心的内部输出状态，所以进记录；`CmdWr_Rdy` 由主机驱动、是核心的输入，核心不能「记住」它（它是别人当下的状态），因此不进记录，而是直接进 `p_comb` 的敏感表。

**练习 2**：`PatternCnt` 与 `CmdWr_Size` 都是 `unsigned(AxiAddrWidth_g-1 downto 0)`，它们是什么关系？

**答案**：`CmdWr_Size` 是「本轮一共要写多少拍」（命令参数），`PatternCnt` 是「已经写到第几拍」（进度）。`Write_s` 状态里用 `r.PatternCnt = r.CmdWr_Size-1` 判定是否写到最后一拍（见 u3-l3）。

---

### 4.3 组合进程与寄存进程

#### 4.3.1 概念说明

有了记录与双信号，两进程的骨架就清晰了：

- **组合进程 `p_comb`**：纯组合逻辑。它读 `r`（当前态）与所有相关输入，算出 `r_next`（下一态）。它**不碰时钟、不碰复位**——时钟沿的事交给 `p_reg`。
- **寄存进程 `p_reg`**：只对 `Clk` 敏感。每个上升沿把 `r_next` 抄进 `r`；若复位有效，则只覆盖少数关键字段。

`p_comb` 里有一个决定整段代码风格的惯用法——**`v := r`**。它声明一个记录型**变量** `v`，开头先把当前态 `r` 整体复制给 `v`，之后所有逻辑只改 `v` 里**需要变**的字段，最后 `r_next <= v`。好处是：你在某个状态分支里只写「这一拍要变化的量」，其余字段因 `v := r` 而自动保持——**等价于「寄存器默认保持、只在条件满足时改写」的直觉**，代码因此短得多。

关于复位：`p_reg` 采用**同步复位**（在 `if rising_edge(Clk)` 内部再判 `Rst`），且**只复位少数字段**——`Fsm` 与若干 valid/ready 握手信号。这是有意为之的资源取舍，下一节解释原因。

#### 4.3.2 核心流程

两进程在一个时钟周期内的协作时序：

```
时钟沿 t:   p_reg 把 r_next(t) → r(t)            [抄写]
            对外端口 = f(r(t))                    [并发赋值，寄存后输出]

周期内:     p_comb 读 r(t) + inputs(t)
            → 计算 v（默认 v:=r，再按 FSM 改若干字段）
            → r_next <= v                          [产出下一态]

时钟沿 t+1: r_next(t) → r(t+1)                    [下一拍生效]
```

写成递推式：

\[ r^{(t+1)} = r_{\text{next}}^{(t)} = f\bigl(r^{(t)},\ \text{inputs}^{(t)}\bigr), \qquad \text{outputs}^{(t)} = g\bigl(r^{(t)}\bigr) \]

`f` 是 `p_comb` 实现的组合函数，`g` 是并发赋值实现的输出映射。这就是一台**输出寄存型（Moore 风格）有限状态机**的标准描述。

#### 4.3.3 源码精读

**组合进程 `p_comb` 的敏感表**：

[hdl/mem_test.vhd:127-128](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L127-L128) —— 敏感量包含 `r`（当前态）与所有它要读的输入：`Reg_Rd/Reg_Wr/Reg_WData`（寄存器侧）、`Wr_Error/Rd_Error`（响应侧）、`CmdWr_Rdy/WrDat_Rdy/CmdRd_Rdy`（写/读命令与写数据就绪）、`RdDat_Vld/RdDat_Data`（读数据）。**注意没有 `Clk`、没有 `Rst`**——它是纯组合进程。

**关键惯用法 `v := r`**：

[hdl/mem_test.vhd:129](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L129) 与 [hdl/mem_test.vhd:143](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L143) —— 先声明变量 `v : two_process_r`（L129），再用 `v := r;`（L143）整体复制当前态。此后 FSM 的 `case r.Fsm is` 各分支只修改 `v` 里要变化的字段。**每个周期开始处还会把握手类有效信号先清零**（[hdl/mem_test.vhd:200-205](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L200-L205)：`v.CmdWr_Vld := '0'; v.WrDat_Vld := '0'; v.CmdRd_Vld := '0'; v.RdDat_Rdy := '0';`），再由具体状态按需拉高——这种「先清零、再按条件置 1」是产生**单拍脉冲**的标准手法（避免 valid 卡在高电平发成多拍命令）。

**收尾把 `v` 写给 `r_next`**：

[hdl/mem_test.vhd:361](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L361) —— `r_next <= v;`。组合进程的全部产出都汇聚到这一句。

**输出端口的并发赋值**（从 `r` 引出，寄存后输出）：

[hdl/mem_test.vhd:364-375](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L364-L375) —— 每个对外输出端口都直接读 `r` 的某字段：`CmdWr_Addr <= std_logic_vector(r.CmdWr_Addr);`、`CmdWr_Vld <= r.CmdWr_Vld;`、`WrDat_Data <= std_logic_vector(r.Pattern);`（注意写数据就是 pattern）、`WrDat_Be <= (others => '1');`（字节使能恒全开——本 IP 总是整字写）、`CmdWr_LowLat <= '0';`（不用低延迟模式，恒 0）等。因为读的是 `r`（已寄存），所以**所有输出都是寄存后的**，对下游时序友好。

**寄存进程 `p_reg`**：

[hdl/mem_test.vhd:380-393](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L380-L393) —— 只对 `Clk` 敏感。`if rising_edge(Clk)` 内先 `r <= r_next;`（无条件抄写下一态），再 `if Rst = '1'` 覆盖少数字段：`r.Fsm <= Idle_s;`、`r.CmdWr_Vld/WrDat_Vld/CmdRd_Vld/RdDat_Rdy <= '0';`、`r.ContRunning <= '0';`。这就是**同步、部分复位**。

> `Rst` 信号本身由 `Rst_n` 取反得到（[hdl/mem_test.vhd:117](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L117) 声明、[hdl/mem_test.vhd:122](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L122) `Rst <= not Rst_n;`）——对外是低有效 `Rst_n`，内部转成高有效 `Rst` 方便书写。

#### 4.3.4 代码实践

**实践目标**：解释「为什么命令 valid 信号在 `p_reg` 复位里必须清零，而统计/数据字段不清」（本讲指定实践任务的下半部分）。

**操作步骤（源码阅读型）**：

1. 列出 `p_reg` 复位覆盖的字段：`Fsm`、`CmdWr_Vld`、`WrDat_Vld`、`CmdRd_Vld`、`RdDat_Rdy`、`ContRunning`（见 [hdl/mem_test.vhd:384-390](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L384-L390)）。
2. 列出**未**被复位覆盖的字段：`Errors`、`FirstErrAddr`、`FirstErrFound`、`Pattern`、`PatternCnt`、`CmdWr_Addr`、`CmdWr_Size`、`CmdRd_Addr`、`CmdRd_Size`、`ContIter`。
3. 把这两份清单与「字段分类表」对照，归纳规律：被复位的 = **控制流与对外握手**；未复位的 = **统计/数据/地址参数**。
4. 回答关键问题：若 `CmdWr_Vld` 复位时不清零，会怎样？

**需要观察的现象 / 解释**：

- **为什么 valid/ready 必须清零**：`CmdWr_Vld/WrDat_Vld/CmdRd_Vld` 是发给主机的「命令/数据有效」握手。若上电后（或在复位解除瞬间）它意外为 1，主机会把一条**还没配置好的垃圾命令**当成有效事务，立刻向 DDR 发起一次无意义的 AXI 写/读——这是典型的「上电毛刺触发误操作」。清零保证：在 FSM 主动拉高之前，绝不向下游发任何命令。`RdDat_Rdy` 同理——若上电为 1，核心会在自己还没准备好时声称「能收读数据」，吞下来路不明的数据。
- **为什么 `Fsm` 必须复位到 `Idle_s`**：枚举类型未初始化时取 `'U'`，`case r.Fsm is` 会落到 `when others`（在 FSM 里 `when others => IntError_s`），导致一上电就进错误态。复位到 `Idle_s` 确保从已知起点开始。
- **为什么统计/数据字段可不复位**：这些字段只有在 FSM 进入运行态、被显式赋值后才被使用。例如 `Errors/FirstErrAddr/FirstErrFound/ContIter` 在 START 触发时被一并清零（[hdl/mem_test.vhd:217-220](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L217-L220)），`Pattern/PatternCnt` 在命令态被 `InitPattern` 初始化（u3-l4）。也就是说**软件 START 流程自带「软复位」**，硬件复位省掉它们不影响正确性，却省下了带复位端的触发器资源（复位网络更小、布线更省）。

**预期结果**：复位策略可概括为一句话——**「只复位会向外发命令或决定控制流的信号，统计/数据类留给 START 流程清零」**。本实践为源码阅读型，结论可直接从源码推出；如要在仿真里观察「不复位 valid 会导致误命令」，需在 testbench 里人为去掉复位语句后跑——「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`p_comb` 的敏感表里为什么没有 `Clk`？把它加进去会怎样？

**答案**：`p_comb` 是组合进程，输出应只由 `r` 与输入决定，与时钟无关。把 `Clk` 加进敏感表不会改变综合结果（综合器按组合逻辑实现），但在**门级仿真**里会导致 `r_next` 在每个时钟沿额外求值一次，可能造成仿真与综合行为不一致，是常见的坑。

**练习 2**：如果把 `p_reg` 里那句 `r <= r_next;`（[hdl/mem_test.vhd:383](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L383)）删掉、改成在每个状态分支里直接给 `r.xxx` 赋值，会丢失什么？

**答案**：会丢失 two-process 法的核心好处——`v := r` 带来的「只写变化量、其余自动保持」。改成逐字段直接赋值后，每个分支都必须把所有应保持的字段重述一遍，代码膨胀且易漏（漏一个就变成意外清零）。两进程法的意义正在于把「保持」这件琐事交给 `v := r` + `r <= r_next` 自动处理。

**练习 3**：`WrDat_Be <= (others => '1');` 说明本 IP 怎么写存储器？有没有「只写半字」的能力？

**答案**：字节使能恒为全 1，意味着每次写都是整字（全部字节）有效，不利用 AXI4 的字节使能做局部写。本 IP 的测试场景（写完整 pattern 再整段读回比对）不需要局部写，故简化为恒全开。

## 5. 综合实践

**任务**：用本讲的三块积木（entity 契约、记录分类、两进程协作）做一次「给定状态快照，预测输出与下一态」的纸面推演，把知识串起来。

情景：假设当前 `r.Fsm = Write_s`、`r.WrDat_Vld = '1'`、`r.PatternCnt = 5`、`r.CmdWr_Size = 8`、`r.Pattern = X"0000_000A"`，且此刻输入 `WrDat_Rdy = '1'`。

请完成：

1. **预测对外输出**：根据 [hdl/mem_test.vhd:364-375](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L364-L375)，写出本拍 `WrDat_Data`、`WrDat_Vld`、`WrDat_Be` 的值。（预期：`WrDat_Data = X"0000_000A"`、`WrDat_Vld = '1'`、`WrDat_Be = 全 1`。）
2. **判断是否握手成功**：`WrDat_Vld` 与 `WrDat_Rdy` 是否同拍为 1？若是，这一拍 pattern 被写出去。
3. **预测下一态变化**：读 [hdl/mem_test.vhd:237-253](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L237-L253) 的 `Write_s` 分支。`PatternCnt(5) = CmdWr_Size-1(7)`？不等，所以走 else：`PatternCnt` 加 1 变 6、`UpdatePattern_v := true`（pattern 更新，具体算法 u3-l4）。`WrDat_Vld` 因 [hdl/mem_test.vhd:201](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L201) 的「先清零」与 [hdl/mem_test.vhd:238](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L238) 的「再置 1」仍为 1，下一拍继续写。
4. **边界情形**：若把 `r.PatternCnt` 改成 7（即最后一拍），走 [hdl/mem_test.vhd:241-247](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L241-L247）的「最后一拍」分支：模式不是 WRITEONLY 时 `v.Fsm := RdCmd_s`、`v.WrDat_Vld := '0'`——写阶段结束，转入读命令态。

**交付物**：一张「输入快照 → 本拍输出 → 下一拍 `r` 变化」的三列表，并在末尾用一句话点出 two-process 法在其中扮演的角色（即：你只需在 `Write_s` 分支里写「PatternCnt+1、UpdatePattern」，其余保持全靠 `v := r`）。

> 提示：本实践是纯纸面推演，不需任何工具；若想用仿真核对，可在 testbench 里强制 `r` 到上述快照后单步运行，结果「待本地验证」。

## 6. 本讲小结

- `mem_test` 实体的对外契约分三组端口：控制（`Clk/Rst_n`）、寄存器接口（`Reg_Rd/Reg_RData/Reg_Wr/Reg_WData`）、AXI 主机用户接口（`CmdWr_*/CmdRd_*/WrDat_*/RdDat_*` + 四路响应）。
- 两个 generic `AxiAddrWidth_g(12~64)`、`AxiDataWidth_g(16~1024)` 用 `range` 把合法配置钉死，默认都是 32；entity 上方的 `-- $$ ... $$` 是 psi_tb/PsiSim 框架解析的元数据注释。
- 命令/数据端口全是 valid/ready 握手：写侧 `*_Vld` 是输出、读侧 `RdDat_Vld` 是输入；唯一由核心驱动的 Rdy 是 `RdDat_Rdy`。
- 所有内部状态打包进记录 `two_process_r`（16 字段，五大类：状态机、错误统计、pattern/进度、命令/数据输出与握手、continuous 模式），用 `r`（当前态）/`r_next`（下一态）双信号承载。
- two-process 法：`p_comb`（纯组合）读 `r` 与输入、用 `v := r` 惯法「只写变化量」、最后 `r_next <= v`；`p_reg`（只对 `Clk` 敏感）每沿 `r <= r_next` 并做**同步、部分复位**。
- 复位只覆盖控制流与握手信号（`Fsm→Idle_s`、各 `*_Vld/RdDat_Rdy/ContRunning→0`）；统计/数据字段不复位，靠 START 流程的「软清零」兜底，省下复位触发器资源——这正是命令 valid 必须清零、而 `Errors/Pattern` 不必清的根本原因。

## 7. 下一步学习建议

本讲搭好了核心实体的骨架，接下来把血肉填进去：

- **u3-l3 主状态机**：精读 `case r.Fsm is` 的每个分支，把 Idle→WrCmd→Write→RdCmd→Read 的完整流转、`ContRunning` 的循环回跳、`AxiError_s/IntError_s` 的不可恢复错误画成状态转移图。本讲里 `FsmToInt` 函数（[hdl/mem_test.vhd:77-89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89)）如何把内部细状态合并成对外 STATUS 码，也在那一讲展开。
- **u3-l4 pattern 生成与校验**：深入 `InitPattern_v`/`UpdatePattern_v` 两个标志触发的四种 pattern 算法（Counter/Walk1/OwnAddr/PRBN），以及 `Read_s` 里读回比对、`Errors` 累加与 `FirstErrAddr` 的换算。
- **u4-l2 AXI4 主机命令/burst**：本讲里 `CmdWr_Size` 的 `shift_right(..., log2(AxiDataWidth_g/8))`（[hdl/mem_test.vhd:227](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L227)）把字节数折算成 beat 数，那一讲会补全它如何映射到 AXI4 burst。
