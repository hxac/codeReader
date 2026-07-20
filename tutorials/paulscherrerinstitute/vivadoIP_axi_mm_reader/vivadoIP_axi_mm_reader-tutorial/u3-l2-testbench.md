# 测试台架构与用例

## 1. 本讲目标

本讲把视线从「IP 本身怎么工作」转到「我们凭什么相信它工作正确」。`tb/top_tb.vhd` 是这个 IP 唯一一份、也是自校验（self-checking）的回归测试台。学完后你应当能够：

- 说出**自校验测试台**的三大组成部分：被测对象（DUT）、扮演 AXI 主机的配置侧、扮演 AXI 从机（被读设备）的响应侧，以及它们各自挂在 DUT 的哪个端口上。
- 看懂测试台用两组 AXI **BFM（Bus Functional Model，总线功能模型）记录** `axi_ms/axi_sm` 与 `axi_ms_m/axi_sm_m` 把完整的 AXI4 五通道事务打包传递的方式。
- 解释 `p_control`（激励进程）与 `p_spi`（响应进程）两个并发进程如何用一对整数信号 `StimCase`/`RespCase` 做**握手套同步**，从而把「谁来读、读到什么」切成 6 个独立用例。
- 读懂 `CheckResults` 过程如何根据顶层 generic `OutputType_g` 在 **AXIS** 与 **AXIMM** 两条校验路径之间分发。
- 逐个说出 6 组用例（单次读、缓冲双读、超时、禁用、背压、单寄存器四次读）各自验证了 IP 的什么行为。
- 能够在 `top_tb.vhd` 里增加或修改一个用例，并知道是否需要同步改动 `sim/config.tcl`。

## 2. 前置知识

本讲是「看懂测试」的讲义，不是「看懂 RTL」的讲义。它会反复引用前面已经建立的硬件认知，但不再重复推导。请确认你已熟悉下面两讲：

- **u2-l1 整体架构与数据流**：IP 黑盒对外有 `s00_axi`（从机，软件配置）、`m00_axi`（主机，只读不写，主动读别人）、`m_axis` 或 `RdData`（送出结果）三类接口，外加 `Trig` 单拍启动脉冲与 `DoneIrq` 完成脉冲。本讲的 DUT 就是这套完整黑盒（即 wrapper），测试台分别在这三类接口上挂了 BFM。
- **u2-l3 核心 FSM：双进程状态机**：核心 FSM 有 `Idle_s/ReadAddr_s/SetCmd_s/ApplyCmd_s/WaitDone_s` 五态；`Trig` 只在 `Idle_s` 被消费，进行中的 `Trig` 被丢弃；`DoneCnt` 收齐 `RegCount` 个字后发 `DoneIrq`。本讲的「背压」「禁用」用例正是在反复挤压这条 FSM 的边界条件。

另外还需要几项测试台领域的常识：

- **BFM（Bus Functional Model）**：把一套总线协议（这里是 AXI4）的握手时序封装成高层过程调用的模型。有了 BFM，测试台不必手写每一根 `arvalid/arready/...` 的翻转，而是调用 `axi_single_write(地址, 数据, ...)` 这样的过程，由 BFM 在底层把事务拆成通道信号。本仓库的 BFM 来自外部库 `psi_tb`（`psi_tb_axi_pkg`）。
- **自校验测试台**：测试台自己既产生激励、又检查结果，发现不符就调用 `assert`/比较函数报错（在 transcript 里打出 `###ERROR###`），不需要人工看波形。这正好配合 u1-l3 讲过的 CI 判定：`run_check_errors "###ERROR###"` 会扫描 transcript 里的错误标记。
- **record 信号**：VHDL 的 `record` 类型把多个信号打包成一个整体。`psi_tb_axi_pkg` 定义了 `axi_ms_r`（master 侧输出）与 `axi_sm_r`（slave 侧输出）两种 record，把 AXI4 的 AR/W/R/B 五通道信号全收进去，测试台只传一个信号名就够了。
- **并发进程**：VHDL 的 `process` 彼此并发执行。本测试台用三个进程：时钟、激励、响应，靠信号在进程间传递信息。

> 一个容易绊倒人的细节：测试台里有一个进程名叫 `p_spi`（注释写作 `SPI Emulation`），但它**并没有任何 SPI 时序**。这是一个历史遗留的命名——它真正扮演的角色是「`m00_axi` 主机端口所读的那些 AXI 从机设备」，即被读取寄存器的仿真替身。读源码时把 `p_spi` 在脑子里改写成 `p_axi_subordinate_emu` 就不会误解。

## 3. 本讲源码地图

本讲只围绕两个文件展开，但会顺带引用一个 RTL 包做地址交叉验证：

| 文件 | 作用 |
| --- | --- |
| `tb/top_tb.vhd` | 唯一的测试台，471 行，自校验。实体带一个 generic `OutputType_g`，内部实例化 DUT、生成时钟、跑 `p_control` 与 `p_spi` 两个进程，共 6 组用例。 |
| `sim/config.tcl` | PsiSim 仿真配置：声明编译哪些源文件（分 `lib/src/tb` 三组），并用 `create_tb_run` 让同一个 `top_tb` 以 `OutputType_g=AXIS` 与 `AXIMM` 两组 generic 各跑一次。 |
| `hdl/definitions_pkg.vhd` | 寄存器字索引常量（`RegIdx_Ctrl_c` 等），测试台用它把字索引换算成字节地址。 |

整个测试台没有更多文件——它把 DUT 实例化、BFM 调用、激励、校验全部写在 `top_tb.vhd` 一个 `architecture sim` 里。这与 u1-l2 讲过的「每目录文件很少、聚焦」的仓库风格一致。

## 4. 核心概念与源码讲解

### 4.1 自校验测试台骨架与 AXI BFM

#### 4.1.1 概念说明

要测试一个挂在 AXI 总线上的 IP，最朴素的办法是手写每个时钟边沿上每根 AXI 信号的值。这既繁琐又容易把「激励写错」与「DUT 错」混在一起。自校验测试台的做法是：

- **把 DUT 当黑盒**，只在它的三类对外接口上接「替身」。
- 在 `s00_axi`（从机）一侧挂一个**AXI 主机 BFM**，扮演软件：它替我们发配置写、读状态寄存器。
- 在 `m00_axi`（主机）一侧挂一个**AXI 从机 BFM**，扮演被读设备：它替我们「接住」DUT 发来的读地址、回送约定的读数据。
- 在 `m_axis`（AXIS 输出）一侧直接由测试台进程驱动 `tready`、检查 `tdata/tlast`。
- 激励与期望结果都写在测试台代码里，由 BFM 与比较函数自动判定，发现不符即报错。

这样测试台代码读起来几乎是「先写这段配置 → 触发 → 期望读到 0,1,2,…」的自然语言描述，AXI 握手的脏活全被 BFM 藏掉。

#### 4.1.2 核心流程

一次典型用例在测试台里的数据回路：

```text
p_control (AXI 主机 BFM)
   │  axi_single_write(RegIdx_RegCnt_c*4, 14, ...)   写 RegCnt=14
   │  axi_single_write((MemOffs_c+i)*4, 0x00AB0000+16*i, ...)  填 RegTable
   │  axi_single_write(RegIdx_Ctrl_c*4, 1, ...)      使能
   │  PulseSig(Trig, aclk)                           单拍触发
   ▼
DUT (axi_mm_reader_wrp)
   │  FSM 遍历 RegTable，经 m00_axi 逐个读 0x00AB0000+16*i
   ▼
p_spi (AXI 从机 BFM)
   │  axi_expect_ar(0x00AB0000+16*i, ...)             接住读地址
   │  axi_apply_rresp_single(i, xRESP_OKAY_c, ...)    回送数据 i
   ▼
DUT 把读回值压入内部 FIFO，按输出模式交出
   ▼
p_control 再用 CheckResults(...) 取数并比较，期望 0,1,2,…,13
```

注意数据值是「绕一圈」的：`p_control` 写进 RegTable 的是**地址**（`0x00AB0000+16*i`），DUT 去读这些地址，`p_spi` 回送的**数据**是 `i`，最后 `CheckResults` 校验的也是 `i`。三者通过同一个下标 `i` 串起来，所以一眼就能看出「读到的值是否对应正确的地址」。

#### 4.1.3 源码精读

测试台实体只暴露一个 generic——它正是切换双模式的开关：

[tb/top_tb.vhd:L27-L31](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L27-L31) —— `OutputType_g : string := "AXIMM"`。这个字符串会被原样透传给 DUT 的 `Output_g`（见下方 generic map），并在 `CheckResults` 里决定走哪条校验路径。默认值是 `AXIMM`，但 `sim/config.tcl` 会让它以两个值各跑一次。

AXI 总线宽度在测试台里写死成常量：

[tb/top_tb.vhd:L38-L48](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L38-L48) —— `ID_WIDTH=1`、`ADDR_WIDTH=8`、`DATA_WIDTH=32`，并据此定义了若干 `subtype` 作为 record 字段的范围约束。注意 `s00_axi` 用 8 位地址（与 DUT 的 `AxiSlaveAddrWidth_g => 8` 对齐），而 `m00_axi` 的地址是 32 位（因为被读的寄存器地址 `0x00AB0000` 是 32 位）。

**两组 BFM 记录**是本节的核心。测试台声明了四个 record 信号：

[tb/top_tb.vhd:L50-L68](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L50-L68) —— 四个信号分成两组：

| 信号 | 扮演的角色 | 连到 DUT 的哪个端口 |
| --- | --- | --- |
| `axi_ms` | s00_axi 上的 **AXI 主机**输出（AR/W 通道、R/B 通道的 ready） | `s00_axi_*` 输入 |
| `axi_sm` | s00_axi 上的 **AXI 从机**输出（AR/W 的 ready、R/B 通道数据） | `s00_axi_*` 输出 |
| `axi_ms_m` | m00_axi 上的 **AXI 主机**输出（由 DUT 驱动） | `m00_axi_*` 输出（被测试台采样） |
| `axi_sm_m` | m00_axi 上的 **AXI 从机**输出（由测试台驱动） | `m00_axi_*` 输入（回送读数据） |

记忆口诀：`_ms` = master side（主机侧输出），`_sm` = slave side（从机侧输出）；带 `_m` 后缀的属于 `m00_axi`（master 端口那一侧），不带后缀的属于 `s00_axi`（slave 端口那一侧）。所以 `axi_ms` 是「测试台作为主机去配置 DUT 从机」，`axi_sm_m` 是「测试台作为从机去应答 DUT 主机」。

DUT 实例化时把两组 record 拆开接到端口上（VHDL 里 record 不能整个连到标量端口，要逐字段）。generic map 把测试台常量透传给 DUT：

[tb/top_tb.vhd:L157-L165](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L157-L165) —— 注意三个关键映射：`ClkFrequencyHz => integer(ClockFrequencyAxi_c)`（把 125.0e6 的 real 转成整数 125 000 000）、`TimeoutUs_g => 10`、`Output_g => OutputType_g`（双模式开关透传）。`MaxRegCount_g => 16`、`MinBuffers_g => 2` 决定了内部 FIFO 深度为 `16*2=32` 字（见 u2-l7），这一数字在 4.4 的背压用例里会再次出现。

DUT 的端口映射把 `s00_axi`、`m00_axi`、`m_axis` 全部接到测试台信号上：

[tb/top_tb.vhd:L166-L232](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L166-L232) —— `s00_axi_*` 接 `axi_ms`/`axi_sm` 的各字段，`m00_axi_*` 接 `axi_ms_m`/`axi_sm_m`，`m_axis_*` 接测试台自己的 `m_axis_*` 信号。注意 `m00_axi` 只连接了读通道（`araddr/arvalid/...` 与 `rdata/rresp/...`），没有写通道——这与 u2-l6 讲过的「主机只读不写」完全对应。

时钟进程非常朴素，靠 `TbRunning` 布尔信号控制何时停摆：

[tb/top_tb.vhd:L238-L248](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L238-L248) —— 产生 125 MHz（周期 8 ns）的方波；当激励进程在结尾把 `TbRunning <= false`（见 [tb/top_tb.vhd:L393](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L393)）后，`while TbRunning loop` 退出、进程走到 `wait;` 永久挂起，时钟停止，仿真随之结束。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把「DUT 端口 ↔ BFM 记录 ↔ 进程」三者关系亲手对一遍。

1. 打开 [tb/top_tb.vhd:L166-L232](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L166-L232) 的端口映射。
2. 对 `s00_axi_araddr`、`s00_axi_arready`、`s00_axi_rdata` 三个端口，分别写出它接的是 `axi_ms` 还是 `axi_sm` 的哪个字段。
3. 对 `m00_axi_araddr`、`m00_axi_arready`、`m00_axi_rdata` 三个端口，同样写出它接的是 `axi_ms_m` 还是 `axi_sm_m`。
4. 回答：为什么 `m00_axi` 一侧没有 `aw*`/`w*`/`b*` 这些写通道端口？

**预期结果**：

- `s00_axi_araddr` ← `axi_ms.araddr`（主机给出的地址）；`s00_axi_arready` ← `axi_sm.arready`（从机给主机的就绪）；`s00_axi_rdata` ← `axi_sm.rdata`（从机回送的数据）。
- `m00_axi_araddr` ← `axi_ms_m.araddr`（DUT 作为主机给出的地址）；`m00_axi_arready` ← `axi_sm_m.arready`（测试台扮演的从机给的就绪）；`m00_axi_rdata` ← `axi_sm_m.rdata`（测试台回送的数据）。
- 第 4 问：因为 DUT 的主机被配置成「只读不写」（`ImplWrite_g=false`，见 u2-l6），wrapper 实体根本没声明写通道引脚，测试台自然也不必连。

#### 4.1.5 小练习与答案

**练习 1**：`s00_axi` 用 8 位地址，`m00_axi` 却用 32 位地址，为什么不一样？

**参考答案**：`s00_axi` 是软件配置这个 IP 用的从机，它寻址的是 IP 自己那段很小的寄存器空间（最多到 `0x20` 起的 RegTable，几十字节量级，8 位 = 256 字节够用，参见 u2-l2 的地址宽度下限推导）。`m00_axi` 是 IP 去读别人用的主机，被读的寄存器地址（如本测试台里的 `0x00AB0000`）是 32 位系统地址，所以测试台把 `axi_ms_m.araddr` 声明成 `31 downto 0`（见 [tb/top_tb.vhd:L60-L64](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L60-L64)）。

**练习 2**：如果要让仿真跑得更快（少占机时），本测试台最现成的手段是什么？

**参考答案**：降低时钟频率。注释里写明 `ClockFrequencyAxi_c := 125.0e6` 旁有一句 `-- Use slow clocks to speed up simulation`（[tb/top_tb.vhd:L74](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L74)）。这里「快」指仿真器实时，而非 DUT 时钟——慢时钟让同样数目的仿真周期对应更长的「挂钟时间」，便于用 `1 us`、`8 us` 这类人类友好的延时来描述超时与间隔，同时这个频率值会通过 `ClkFrequencyHz` 透传给 DUT 参与超时换算（见 4.4 超时用例）。改这个值时必须同步意识到它影响 DUT 的超时周期换算。

---

### 4.2 双进程握手：StimCase / RespCase 同步机制

#### 4.2.1 概念说明

测试台里有两类完全独立的「对话」要协调：

1. **激励侧**（`p_control`）：决定「现在该触发、该写什么配置、该期待什么输出」。
2. **响应侧**（`p_spi`）：决定「DUT 来读 m00_axi 时，我该回送什么数据」。

这两件事不能写在一个进程里，因为它们并发发生——`p_control` 发完 `Trig` 后要**同时**等 `m_axis` 输出**和**应付 DUT 在 `m00_axi` 上的读请求。把它们拆成两个并发进程是最自然的写法。

但拆成两个进程立刻带来一个问题：它们怎么知道「现在该跑哪一个用例」？如果用例 1 还没结束，`p_spi` 就提前进入用例 2 的等待，就会错过用例 1 的读请求。本测试台用一个极简但很巧妙的约定解决：

- `p_control` 在开始一个用例前，把信号 `StimCase` 赋成用例编号。
- `p_spi` 用 `wait until rising_edge(aclk) and StimCase = N` 卡住，直到看到自己的编号才动手。
- `p_spi` 做完该用例的全部响应后，把信号 `RespCase` 赋成同一个编号。
- `p_control` 在用例末尾用 `wait until rising_edge(aclk) and RespCase = N` 卡住，确认对方做完，再进入下一用例。

于是 `StimCase` 是「主→从：该你做 N 了」，`RespCase` 是「从→主：我做完了 N」。两个整数信号就构成了一对**阻塞握手**，把 6 个用例串成一条严格顺序的流水线。

#### 4.2.2 核心流程

两个进程的时序关系（以一个用例 N 为例）：

```text
p_control                                p_spi
─────────                                ─────
StimCase <= N
                                         wait until StimCase = N   (解除阻塞)
PulseSig(Trig) / 写配置 / CheckResults    axi_expect_ar(...) ×14
                                            axi_apply_rresp_single ×14
                                         RespCase <= N
wait until RespCase = N   (解除阻塞)
StimCase <= N+1
                                         wait until StimCase = N+1  ...
```

两个 `wait until ... and <信号>=N` 就是屏障：任何一个没就位，另一个就原地等待。因为 VHDL 信号更新发生在进程挂起（`wait`）的时刻，两个进程会在时钟上升沿这一统一的时刻点上同步推进，不会出现竞争。

#### 4.2.3 源码精读

握手用的两个信号声明在 TB Definitions 段：

[tb/top_tb.vhd:L74-L78](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L74-L78) —— `StimCase : integer := -1`、`RespCase : integer := -1`。初值取 `-1`（一个不属于任何用例 1..6 的值），确保两个进程起步时都不会被误判成「某个用例已开始/已完成」。

`p_control` 进程的整体形状是「复位 → 公共配置 → 六个用例 → 收尾」：

[tb/top_tb.vhd:L253-L277](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L253-L277) —— 进程开头先拉复位（`aresetn<='0'` 维持 1 µs 后释放），再做**所有用例共享的一次性配置**：写 `RegCnt=14`、循环填 14 项 RegTable（地址 `0x00AB0000+16*i`）、写 `Ctrl=1` 使能。之后才是用例 1。注意这套配置在用例之间不重写（用例 6 才把 `RegCnt` 改成 1）。

[tb/top_tb.vhd:L264-L269](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L264-L269) —— 公共配置段。地址换算 `(MemOffs_c+i)*4` 就是「字索引 (8+i) × 4 = 字节地址」，与 u2-l5 讲过的 `RegCfg_Idx = mem_addr(... downto 2)` 互为逆运算。

每个用例在 `p_control` 里的固定三段式是「`StimCase <= N` → 做激励与校验 → `wait until ... RespCase = N`」。以用例 1 为例：

[tb/top_tb.vhd:L271-L277](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L271-L277) —— `StimCase <= 1` 唤醒 `p_spi`；`PulseSig(Trig, aclk)` 产生单拍触发；`CheckResults(0, 1, ...)` 取数校验；最后 `wait until rising_edge(aclk) and RespCase = 1` 等对方做完。

`p_spi` 进程对应的用例 1：

[tb/top_tb.vhd:L413-L418](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L413-L418) —— `wait until rising_edge(aclk) and StimCase = 1` 阻塞至被唤醒；随后循环 14 次：`axi_expect_ar` 期望 DUT 来读地址 `0x00AB0000+16*i`（并校验 `AxSIZE_4_c`、`arlen=0`、`xBURST_INCR_c` 等 AXI 属性），再用 `axi_apply_rresp_single` 回送数据 `i`、响应码 `xRESP_OKAY_c`；最后 `RespCase <= 1` 通知 `p_control` 收工。

> 关键观察：`axi_expect_ar` 不只是「等一个地址」，它会**断言**这个地址必须出现、且属性必须匹配。所以 `p_spi` 同时承担了「回数据」与「校验 DUT 发出的读事务合不合规」两件事。一旦 DUT 读错了地址或 burst 属性，这里就会报 `###ERROR###`。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证两个进程的握手是「严格成对」的。

1. 在 `p_control`（[tb/top_tb.vhd:L253-L395](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L253-L395)）里数 `StimCase <= N` 与 `wait until ... RespCase = N` 各出现几次、编号各是多少。
2. 在 `p_spi`（[tb/top_tb.vhd:L400-L466](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L400-L466)）里数 `wait until ... StimCase = N` 与 `RespCase <= N` 各出现几次、编号各是多少。
3. 把两侧画成两张表，确认 1..6 一一对齐。

**预期结果**：两侧的编号集合都是 {1,2,3,4,5,6}，且每个编号在 `p_control` 里都是「先 `StimCase<=N` 后等 `RespCase=N`」，在 `p_spi` 里都是「先等 `StimCase=N` 后 `RespCase<=N`」。如果将来你新增用例忘了在某一边加对应的 `StimCase`/`RespCase`，进程会**死锁**（一方永远等不到编号），仿真会卡住直到仿真器的死锁保护介入——这是这套握手最容易出的错。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `StimCase`/`RespCase` 用 `integer` 而不是 `std_logic_vector`？为什么初值是 `-1`？

**参考答案**：用 `integer` 是因为要表达「用例编号」这种小整数，比较时直接写 `StimCase = 1` 比 `std_logic_vector(to_unsigned(1, ...))` 干净得多，也不必预先定宽度。初值 `-1` 是哨兵值：合法用例号是 1..6，`-1` 保证进程启动时 `wait until ... StimCase = N`（N≥1）一定不会立即通过，必须等到 `p_control` 真正发出第一个编号才解除阻塞，避免「上电瞬间两个进程各跑半步」的竞争。

**练习 2**：`wait until rising_edge(aclk) and StimCase = N` 里为什么要带上 `rising_edge(aclk)`？只写 `wait until StimCase = N` 行不行？

**参考答案**：带上 `rising_edge(aclk)` 把求值时刻钉死在时钟上升沿。VHDL 里 `wait until <条件>` 在任何信号变化后都会重新求值条件；如果只写 `wait until StimCase = N`，求值可能发生在信号的增量周期（delta cycle），与 `p_control` 里其它也在等上升沿的语句不同步，容易引入「同一拍里两个进程交错」的微妙时序问题。统一在上升沿求值，两个进程就在同一时刻点对齐，行为可预测。

---

### 4.3 双模式校验过程：CheckResults 的 AXIS / AXIMM 分发

#### 4.3.1 概念说明

u2-l7 讲过 IP 有两种输出模式：AXIS（数据从 `m_axis` 端口直出）与 AXIMM（数据映射到 `RdData`/`RdLast` 寄存器，软件读 `RdData` 才弹 FIFO）。这两种模式下「怎么把读回值取出来校验」是截然不同的两套动作：

- **AXIS**：直接盯 `m_axis_tvalid/tdata/tlast` 信号，驱动 `m_axis_tready` 把数据「拉出来」，逐拍比较。
- **AXIMM**：通过 `s00_axi` 读 `Level` 寄存器轮询 FIFO 水位，等有数据后再读 `RdLast`（peek）、读 `RdData`（pop）。

但两套动作校验的是**同一件事**：「读回的 14 个字是不是 `start, start+step, ..., start+13*step`，且最后一拍的 `Last` 正确」。所以测试台把「要校验什么」与「怎么取数」分开：用例代码只写 `CheckResults(start, step, ...)`，由 `CheckResults` 内部根据 `OutputType_g` 选择取数路径。这样新增/修改用例时不必关心模式差异——这正是同一份测试台能同时覆盖两种输出的关键。

#### 4.3.2 核心流程

分发逻辑极其简洁：

```text
CheckResults(start, step, ...)
   ├── if OutputType_g = "AXIS"  → CheckResultsAxiS(...)   盯 m_axis 端口
   └── else                       → CheckResultsAxiMM(...)  读 RdData/RdLast/Level 寄存器
```

两条路径的取数方式对照：

| 维度 | CheckResultsAxiS | CheckResultsAxiMM |
| --- | --- | --- |
| 取数通道 | `m_axis_tvalid/data/last` 信号 | `s00_axi` 读 `RdData`/`RdLast`/`Level` 寄存器 |
| 节奏控制 | 测试台拉 `m_axis_tready='1'` 主动取 | 轮询 `Level>0` 后逐字读 `RdData` |
| Last 判定 | 直接看 `m_axis_tlast` 信号 | 读 `RdLast` 寄存器（peek，不弹） |
| 每字比较 | `StdlvCompareInt(start+i*step, data, ...)` | `axi_single_expect(RdData*4, start+i*step, ...)` |
| 字数 | 固定 14（`for i in 0 to 13`） | 固定 14 |

注意两路径都硬编码 `for i in 0 to 13 loop`——这与 `p_control` 开头配置的 `RegCnt=14`（[tb/top_tb.vhd:L265](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L265)）一致；用例 6 把 `RegCnt` 改成 1 后，就不再走 `CheckResults` 而是手写 4 次校验（见 4.4）。

#### 4.3.3 源码精读

`CheckResults` 是一个 **VHDL 过程（procedure）**，定义在 `architecture` 的声明区。先看分发主体：

[tb/top_tb.vhd:L135-L150](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L135-L150) —— 根据 `OutputType_g` 字符串二选一调用子过程。因为 `OutputType_g` 是 generic（综合/ elab 时常量），仿真器实际上会在展开时只保留其中一条分支，但源码两段都写出来便于阅读。

AXIS 路径：

[tb/top_tb.vhd:L96-L111](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L96-L111) —— 先把 `rdy <= '1'`（告诉 DUT「我准备好收了」），然后循环 14 次：`wait until rising_edge(clk) and vld='1'` 等到 DUT 给出有效数据，用 `StdlvCompareInt(start+i*step, data, "Data")` 比较 `tdata`，用 `StdlCompare(choose(i=13,1,0), last, "Wrong Tlast")` 比较 `tlast`——只在最后一拍（`i=13`）期望 `tlast='1'`。循环结束把 `rdy <= '0'`。这里的 `choose` 是个三元函数：`choose(条件, 真值, 假值)`。

AXIMM 路径：

[tb/top_tb.vhd:L113-L133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L113-L133) —— 循环 14 次，每次先用一个内层 `loop` 轮询 `Level` 寄存器（`axi_single_read(RegIdx_Level_c*4, x, ...)`），直到 `x > 0`（FIFO 里有字）才退出；随后 `axi_single_expect(RegIdx_RdLast_c*4, choose(i=13,1,0), ..., "Last")` **先读 `RdLast`**（peek，仅看末值标志），再 `axi_single_expect(RegIdx_RdData_c*4, start+i*step, ..., "Data")` **后读 `RdData`**（pop，弹出 FIFO 一项）。这一「先 `RdLast` 后 `RdData`」的顺序正是 u2-l2 / u3-l1 反复强调的硬件约定——如果颠倒，`RdData` 会先把 FIFO 弹掉，`RdLast` 看到的就不再是当前字的标志。

地址常量来自 RTL 包，确保测试台与 DUT 用同一张地图：

[hdl/definitions_pkg.vhd:L25-L35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L25-L35) —— `RegIdx_Level_c=4`、`RegIdx_RdLast_c=3`、`RegIdx_RdData_c=2`。测试台里 `RegIdx_Level_c*4 = 0x10`、`RegIdx_RdLast_c*4 = 0x0C`、`RegIdx_RdData_c*4 = 0x08`，与 u2-l2 文档表里的 `Level@0x10`、`RdLast@0x0C`、`RdData@0x08` 完全吻合。

> 一个值得品味的设计：`CheckResults` 是**过程**而非进程。它在 `p_control` 进程内被顺序调用，共享 `p_control` 的执行上下文与 `aclk`。这意味着取数期间 `p_control` 是「阻塞」在 `CheckResults` 里的——这正是我们想要的：取完一包再继续下一动作。`p_spi` 在此期间由 `StimCase` 信号同步，独立服务 `m00_axi` 的读请求。

#### 4.3.4 代码实践（源码阅读型）

**目标**：亲眼看两条路径对「同一组期望数据」的两种取法。

1. 打开 [tb/top_tb.vhd:L96-L111](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L96-L111)（AXIS）与 [tb/top_tb.vhd:L113-L133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L113-L133)（AXIMM）。
2. 假设调用是 `CheckResults(0, 1, ...)`，分别写出两条路径在第 `i=5` 拍期望的数据值与 `Last` 值。
3. 在 AXIMM 路径里，如果把「先 `RdLast` 后 `RdData`」改成「先 `RdData` 后 `RdLast`」，描述会发生什么。

**预期结果**：

- `i=5` 时数据 = `start + i*step = 0 + 5*1 = 5`；`Last = choose(5=13, 1, 0) = 0`（非末拍）。两条路径期望完全相同。
- 颠倒顺序后：先读 `RdData` 会把 FIFO 弹出当前字，紧接着读 `RdLast` 看到的已经是**下一个**字的末值标志；于是从第二拍起 `Last` 与 `Data` 错位对齐，校验大概率报错（除非 FIFO 恰好为空导致 `Level` 轮询行为异常）。这就是约定「先 `RdLast` 后 `RdData`」必须被严格遵守的原因。

#### 4.3.5 小练习与答案

**练习 1**：`CheckResults` 为什么写成过程（`procedure`）而不是独立进程（`process`）？

**参考答案**：因为校验是 `p_control` 用例流程里**顺序的一环**——做完校验才能进入下一动作。写成过程，它就在调用方进程内按顺序执行，自然串在「触发 → 校验 → 等对方完成」的链条里；如果写成独立进程，就得再引入一对类似 `StimCase`/`RespCase` 的信号去同步「开始校验/校验完成」，徒增复杂度。过程还带入了 `vld/rdy/last/data/clk` 等信号参数，使同一份逻辑既能给 `m_axis` 用、也能在别处复用。

**练习 2**：AXIS 路径里 `rdy <= '1'` 在循环之前一次性置位，循环结束才 `rdy <= '0'`。这意味着测试台「全速取数」。如果改成「每收到一个字就插一拍 `rdy='0'`」，会触发 IP 的什么行为？

**参考答案**：会触发背压。`m_axis_tready`（即 `AxiS_Rdy`）拉低后，核心 FIFO 停止出队（u2-l7），FIFO 水位上涨；当 FIFO 满时，`Fifo_Rdy` 回灌成 `AxiM_RdDat_Rdy=0`，反过来背压 `m00_axi` 主机（u2-l6）。这正是 4.4 背压用例想要覆盖的场景——不过那个用例是用「触发过快」而非「取数过慢」来制造背压。

---

### 4.4 六组激励/响应用例逐个剖析

#### 4.4.1 概念说明

6 个用例覆盖了 IP 的全部关键行为。每个用例都是「`p_control` 做某件激励 + 校验，`p_spi` 配合回送数据」。下表先给全景，随后逐个细看。

| # | 用例名 | p_control 关键动作 | p_spi 关键动作 | 验证的 IP 行为 |
| --- | --- | --- | --- | --- |
| 1 | Trigger Single Read | `Trig` 单拍触发，`CheckResults(0,1)` | 回送 0..13 | 一次普通读周期，14 个地址/数据/Last 全对 |
| 2 | Buffered Double Read | 两个 `Trig` 相隔 1 µs，先查 `Level=28`，再两包 `CheckResults(0,1)`、`CheckResults(32,1)` | 两轮各回送 0..13、32..45 | FIFO 能缓冲多个完整包，不丢数据 |
| 3 | Timeout | **不触发**，等超时自动到来 | 回送 0..13 | 仅靠超时即可周期性启动读周期 |
| 4 | Disabled | 禁用→触发→观察无活动→重新使能 | 不期望任何 AR | 禁用时忽略 `Trig`、冻结超时计数器 |
| 5 | Back Pressure | 连发 6 个 `Trig` 制造背压，逐包校验 | 循环服务直到 10 µs 无 AR | 背压下数据不丢失、发出的包都完整 |
| 6 | Single Reg Read Four Times | `RegCnt=1`，连发 4 个 `Trig` | 4 次回送 0,1,2,3 | 单寄存器读、1 字包也正确置 `Last` |

#### 4.4.2 核心流程（数据值的「绕一圈」约定）

每个用例的数据值都遵循同一个下标约定，理解了它就读懂了所有用例的期望值：

- `p_control` 把 RegTable 第 `i` 项写成地址 `0x00AB0000 + 16*i`（用例 1/2/3/5；用例 6 只用第 0 项）。
- DUT 经 `m00_axi` 去读这些地址，`p_spi` 用 `axi_expect_ar(0x00AB0000+16*i, ...)` 接住并断言地址正确。
- `p_spi` 回送的数据 = `i`（用例 1/3/5）或 `i + x*32`（用例 2，`x` 是包序号）或 `i`（用例 6，但只读地址 `0x00AB0000`、连发 4 次）。
- `CheckResults(start, step)` 期望的值 = `start + i*step`。

所以「RegTable 里写的地址」与「期望读回的数据」通过 `i` 一一对应，任何一环错了都能被 `axi_expect_ar` 或 `CheckResults` 抓到。

#### 4.4.3 源码精读

**用例 2：缓冲双读（重点，第 5 节还会细走一遍时间线）**

[tb/top_tb.vhd:L279-L291](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L279-L291) —— 先 `axi_single_expect(RegIdx_Level_c*4, 0, ...)` 确认 FIFO 是空的；接着**连续两次** `PulseSig(Trig)`（中间隔 1 µs），两次都不取数；然后 `axi_single_expect(RegIdx_Level_c*4, 14*2, ...)` 断言水位 = 28（两个完整包都已落进 FIFO）；最后才连续两次 `CheckResults` 取出两包，期望分别是 `0..13` 与 `32..45`。

[tb/top_tb.vhd:L421-L428](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L421-L428) —— `p_spi` 用双层循环 `for x in 0 to 1 loop for i in 0 to 13 loop`，期望地址仍是 `0x00AB0000+16*i`（两包读的是同一组地址），但回送数据 `to_unsigned(i+x*32, 32)`——第一包 0..13，第二包 32..45，让两包可区分。

**用例 3：超时**

[tb/top_tb.vhd:L293-L311](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L293-L311) —— 全程**没有** `PulseSig(Trig)`。AXIS 分支：`CheckNoActivity(m_axis_tvalid, 8 us, ...)` 确认前 8 µs 没有输出，`WaitForValueStdl(m_axis_tvalid, '1', 3 us, ...)` 确认随后 3 µs 内 `tvalid` 拉高（即超时到来）。AXIMM 分支：等 5 µs 后查 `Level=0`，再轮询 `Level` 直到 `>0`。超时周期换算：

\[
T_{\text{cycles}} = \left\lfloor \frac{f_{\text{clk}} \cdot T_{\text{us}}}{10^{6}} \right\rfloor
   = \left\lfloor \frac{125 \times 10^{6} \times 10}{10^{6}} \right\rfloor
   = 1250 \text{ 拍}
\]

按 8 ns/拍，\(1250 \times 8\,\text{ns} = 10\,\mu\text{s}\)，所以 `8 µs 无活动 + 3 µs 内到来` 正好框住 10 µs 的超时点。这与 u2-l4 讲的超时换算公式完全一致。

[tb/top_tb.vhd:L431-L436](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L431-L436) —— `p_spi` 仍期望 14 次 AR 并回送 0..13，证明超时确实启动了一个完整的读周期。

**用例 4：禁用**

[tb/top_tb.vhd:L313-L322](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L313-L322) —— 写 `Ctrl=0` 禁用；`PulseSig(Trig)`（触发在禁用态应被忽略）；`CheckNoActivity(m_axis_tvalid, 12 us, ...)` 确认长达 12 µs（已超过 10 µs 超时）都**没有**任何输出——证明禁用不仅忽略 `Trig`，还冻结了超时计数器；再写 `Ctrl=1` 重新使能，`CheckNoActivity(..., 2 us, ...)` 确认重新使能后不会立刻冒出一个读周期（超时计数器要重新计时 10 µs）。

[tb/top_tb.vhd:L439-L440](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L439-L440) —— `p_spi` 在用例 4 **什么都不期望**，直接 `RespCase <= 4`。这本身就是断言：如果 DUT 在禁用态错误地发了读请求，`p_spi` 已离开用例 4 的等待、不会接住，DUT 的 `arvalid` 会挂在那里、后续用例的 `axi_expect_ar` 时序会错乱，从而暴露问题。

**用例 5：背压**

[tb/top_tb.vhd:L324-L355](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L324-L355) —— 连发 6 个 `Trig`（每 1 µs 一个，远快于取数速度），制造 FIFO 满（FIFO 深 32、每包 14 字，两个包即占满）→ DUT 主机被背压、FSM 在 `WaitDone_s` 卡住、后续 `Trig` 被丢弃。然后 `p_control` 慢慢取包：先取一包、再补一个 `Trig`、禁用，再把 FIFO 里**所有剩余完整包**逐一校验（AXIS 用 `while m_axis_tvalid='1'`、AXIMM 用 `while Level>0`），最后重新使能。

[tb/top_tb.vhd:L443-L454](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L443-L454) —— `p_spi` 用 `loop ... wait until arvalid for 10 us; if arvalid='0' exit; ...` 的结构，**持续服务** DUT 发来的读请求，直到 10 µs 内没有新请求才认为「DUT 已被背压到停」。所有回送数据都是 `i`（0..13），所以每一包的期望都是 `CheckResults(0,1,...)`。

**用例 6：单寄存器四次读**

[tb/top_tb.vhd:L357-L388](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L357-L388) —— 先把 `RegCnt` 改成 1；连发 4 个 `Trig`；不走 `CheckResults`（它内部固定 14 字），而是手写 4 次校验，期望 4 个单字包，数据依次 0、1、2、3，且**每个**包的 `Last` 都为 1（即使只有 1 个字）。这验证了「`RegCount=1` 时末拍检测仍然置 `Last`」。

[tb/top_tb.vhd:L457-L462](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L457-L462) —— `p_spi` 期望 4 次地址 `0x00AB0000`（同一个地址读 4 遍），回送 0、1、2、3。

#### 4.4.4 代码实践（源码阅读型）

**目标**：把 6 个用例「验证了什么」自己归纳一遍，作为后面动手改测试台的基础。

1. 对照 [tb/top_tb.vhd:L271-L388](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L271-L388)（`p_control` 六用例）与 [tb/top_tb.vhd:L412-L462](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L412-L462)（`p_spi` 六用例）。
2. 填一张表：`用例号 | p_control 期望的关键值 | p_spi 回送的关键值 | 验证的行为`。
3. 特别指出：用例 5 里 `p_control` 在 AXIS 分支用 `while m_axis_tvalid='1'`、AXIMM 分支用 `while Level>0` 来「吃掉所有剩余包」，这两种「吃到空」的判据为什么必须按输出模式分别写？

**预期结果**：表格见 4.4.1。第 3 问：AXIS 模式下数据从端口流出，FIFO 是否还有料由 DUT 直接表现在 `m_axis_tvalid` 上，所以盯端口；AXIMM 模式下没有 `m_axis` 端口，软件（测试台）只能通过 `Level` 寄存器观察 FIFO 水位，所以盯 `Level`。两种判据对应同一种意图（「把缓冲里所有完整包都取完」），但因输出通道不同而落地不同。

#### 4.4.5 小练习与答案

**练习 1**：用例 4（禁用）里 `p_spi` 直接 `RespCase <= 4` 而不期望任何读请求。如果 DUT 有 bug，在禁用态仍然发了一个读地址，这个 bug 怎么暴露出来？

**参考答案**：`p_spi` 已经跳过用例 4 的处理进入用例 5 的 `wait until StimCase=5`，不会再接住这个多余的读请求。于是 DUT 的 `arvalid` 会一直挂着没人应答（`arready` 不来），`psi_common_axi_master_simple` 内部会等到超时或在后续用例 5 的第一个 `axi_expect_ar` 处出现地址/时序不匹配，进而触发 `axi_expect_ar` 的断言失败或仿真挂起，最终在 transcript 里出现 `###ERROR###` 或被仿真器死锁保护中断。

**练习 2**：用例 2 里两次 `CheckResults` 之间，FIFO 里第二个包是在「第一个包被取走之前」就完整的，还是在取走过程中补上的？依据是什么？

**参考答案**：在取第一个包之前就已经完整。依据是 [tb/top_tb.vhd:L288](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L288) 在两次 `CheckResults`（L289/L290）**之前**就断言 `Level = 14*2 = 28`：两个完整包（共 28 字）都已落进 FIFO。这正是「缓冲」二字的含义——多个读周期可以先行完成并缓存在 FIFO 里，软件（或下游 AXIS）不必在一个周期内就取走。如果 IP 不支持缓冲、第二个包会顶掉第一个，那 `Level=28` 这条断言就会失败。

---

### 4.5 双模式如何各跑一次：sim/config.tcl 的角色

这个小节补上「同一份 `top_tb` 怎么会跑两遍」的最后一环，对应最小模块里「双模式校验过程」的工程落地。

#### 4.5.1 概念说明

`OutputType_g` 是 generic，一次仿真只能取一个值。要同时回归 AXIS 与 AXIMM 两种输出，必须让仿真器把 `top_tb` 以两组不同的 generic 配置各 elaboration 一次。PsiSim 框架提供了 `create_tb_run` + `tb_run_add_arguments` 来声明「同一个测试台、多组 generic 参数」的多轮运行。

#### 4.5.2 源码精读

[sim/config.tcl:L48-L55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L48-L55) —— `create_tb_run "top_tb"` 声明一个测试台运行，`tb_run_add_arguments "-gOutputType_g=AXIS" "-gOutputType_g=AXIMM"` 给出两组 generic，框架据此生成两个独立的仿真场景（共 6 用例 × 2 = 12 个校验路径）。这正是 u1-l3 讲过的「同一测试台跑两遍」的具体实现。

源文件分组与 u1-l3 完全一致：

[sim/config.tcl:L20-L50](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L20-L50) —— `psi_common` 与 `psi_tb` 标 `-tag lib`，本项目 `hdl` 三件标 `-tag src`，`top_tb` 标 `-tag tb`。注意 `psi_tb_axi_pkg`（[sim/config.tcl:L33-L38](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L33-L38)）就是本讲所有 BFM 过程（`axi_single_write`/`axi_expect_ar`/`axi_apply_rresp_single` 等）的来源；`psi_tb_compare_pkg` 提供 `StdlvCompareInt`/`StdlCompare`；`psi_tb_activity_pkg` 提供 `CheckNoActivity`/`WaitForValueStdl`/`ClockedWaitTime`/`PulseSig`；`psi_tb_txt_util` 提供 `print`/`to_string`/`choose`。

> 结论：新增一个测试台文件只需改 `config.tcl` 一处（加一行 `add_sources` 与一条 `create_tb_run`），流程脚本 `run.tcl` 不必动——这与 u1-l3 的结论一致。但如果只是给**现有** `top_tb` 加用例（本讲最常见的改动），则连 `config.tcl` 都不用改，因为 `top_tb.vhd` 已经在编译列表里。

## 5. 综合实践

**任务**：选「缓冲双读」（用例 2），把 `p_control` 与 `p_spi` 两个进程的交互时间线逐步还原，并指出该用例验证了 IP 的哪些行为。这是把第 4 节四个模块串起来的综合练习，**不需要运行仿真**（运行需 PsiSim/Modelsim/GHDL 环境，结果标注为待本地验证），全部基于源码阅读完成。

### 步骤 1：找到用例 2 的两侧代码

- `p_control` 侧：[tb/top_tb.vhd:L279-L291](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L279-L291)
- `p_spi` 侧：[tb/top_tb.vhd:L421-L428](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L421-L428)

### 步骤 2：画出事件时间线

按下表把「谁、在什么时刻、做了什么」逐行填出（顺序已给出，请补全「触发方/作用」一列）：

| 时刻（相对） | 代码行 | 事件 | 触发方 / 作用 |
| --- | --- | --- | --- |
| t0 | L281 `StimCase <= 2` | 把用例号写成 2 | `p_control` → 唤醒 `p_spi` |
| t0 | L421 `wait until StimCase = 2` 解除阻塞 | `p_spi` 进入用例 2 | `p_spi` 响应 |
| t1 | L282 `axi_single_expect(Level*4, 0, ...)` | 读 `Level`，断言为 0 | `p_control` 确认起点 FIFO 为空 |
| t2 | L283-L284 `PulseSig(Trig)` × 第 1 次 | 触发第 1 个读周期 | `p_control` → DUT 启动读 0..13 |
| t2' | L422-L426 `x=0` 内层：`axi_expect_ar`/`apply_rresp` ×14 | 接住 14 个地址、回送 0..13 | `p_spi` 服务第 1 包 |
| t3 | L285-L286 `PulseSig(Trig)` × 第 2 次（隔 1 µs） | 触发第 2 个读周期 | `p_control` → DUT 启动读 32..45 |
| t3' | L422-L426 `x=1` 内层：同样 14 次，回送 32..45 | 接住 14 个地址、回送 32..45 | `p_spi` 服务第 2 包 |
| t4 | L288 `axi_single_expect(Level*4, 14*2, ...)` | 读 `Level`，断言为 28 | `p_control` 确认两包都已入 FIFO |
| t5 | L289 `CheckResults(0, 1, ...)` | 取出第 1 包，校验 0..13 | `p_control` 取数（AXIS 盯端口 / AXIMM 读 RdData） |
| t6 | L290 `CheckResults(32, 1, ...)` | 取出第 2 包，校验 32..45 | `p_control` 取数 |
| t7 | L291 `wait until RespCase = 2` | 等 `p_spi` 收工 | `p_control` 阻塞 |
| t7 | L428 `RespCase <= 2` | 通知用例 2 完成 | `p_spi` → 解除 `p_control` 阻塞 |

### 步骤 3：回答「验证了什么行为」

把用例 2 里每条断言能抓到的 bug 写出来：

1. **缓冲能力**（`Level` 先为 0、两次触发后为 28）：如果内部 FIFO 不能容纳两个完整包（深度 < 28），第二个包会顶掉第一个或根本无法完成第二周期，`Level=28` 断言失败。
2. **数据不丢失**（`CheckResults(0,1)` 与 `CheckResults(32,1)` 都必须通过）：如果第二包覆盖了第一包，或 FIFO 指针错乱，两次 `CheckResults` 至少一次数据不符，`StdlvCompareInt`/`axi_single_expect` 报错。
3. **包边界正确**（两包各自 `Last` 都在末拍）：如果 `Last` 标志没有正确随数据入队（u2-l7 讲的第 33 位），两次取包的末拍 `Last` 校验会失败，可能导致 AXIMM 模式下「先 RdLast 后 RdData」误判包尾。
4. **两包可区分**（`p_spi` 故意回送 `i` 与 `i+32`）：如果 DUT 把两包数据顺序颠倒或混包，期望值与实际值就对不上。
5. **触发不排队但允许紧接**（两次 `Trig` 隔 1 µs）：第一个读周期必须很快完成（14 个单拍读，远小于 1 µs），FSM 回到 `Idle_s` 才能消费第二个 `Trig`；如果 FSM 卡死或第二 `Trig` 被错误丢弃，`p_spi` 的 `x=1` 那轮 `axi_expect_ar` 会等不到、超时失败。

### 步骤 4：观察现象与预期结果

- **预期 transcript**：会打印 `>> Buffered Double Read`（[tb/top_tb.vhd:L280](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L280)），随后没有任何 `###ERROR###`；两轮 `CheckResults` 静默通过（比较函数只在失败时打印）。
- **运行命令**（本地有 PsiSim + Modelsim 时）：按 u1-l3，在 `sim/` 下 `source run.tcl`；它会用 [sim/config.tcl:L54-L55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L54-L55) 的两组 generic 各跑一次，所以用例 2 会在 AXIS 与 AXIMM 两种模式下各执行一遍。
- **若故意制造 bug**（选做）：临时把 [tb/top_tb.vhd:L288](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L288) 的期望 `14*2` 改成 `14`，重跑仿真，应看到 `Level` 比较失败并打印 `###ERROR###`——以此反向验证这条断言确实在起作用。（注意：这是阅读型/验证型练习，修改仅用于本地观察，不应提交。）

> 运行结果受本地仿真环境影响；本仓库提供的是脚本与测试台源码，能否跑通取决于是否已按 u1-l3 准备好 `psi_common`/`psi_tb`/PsiSim。若无法运行，本实践作为「源码阅读与时间线还原」材料同样成立，运行结果标注为**待本地验证**。

## 6. 本讲小结

- `top_tb.vhd` 是一份**自校验**测试台：在 DUT 的三类对外接口上各挂替身——`s00_axi` 上挂 AXI 主机 BFM（`axi_ms/axi_sm`，扮演软件配置侧），`m00_axi` 上挂 AXI 从机 BFM（`axi_ms_m/axi_sm_m`，扮演被读设备），`m_axis` 由测试台直接驱动/采样；BFM 把 AXI4 五通道事务封装成 `axi_single_write`/`axi_expect_ar`/`axi_apply_rresp_single` 等高层过程。
- 两个并发进程 `p_control`（激励与校验）与 `p_spi`（响应被读请求，名字是历史遗留、与 SPI 无关）用一对整数信号 `StimCase`/`RespCase`（初值 `-1`）做**阻塞握手**：主侧置 `StimCase<=N` 唤醒从侧、从侧置 `RespCase<=N` 通知完成，把 6 个用例串成严格顺序。
- `CheckResults` 是个 VHDL **过程**，按 generic `OutputType_g` 在 AXIS（盯 `m_axis` 端口、`StdlvCompareInt` 比较）与 AXIMM（读 `Level` 轮询、先读 `RdLast` peek 再读 `RdData` pop）两条校验路径间分发；两路径对同一组期望值 `start+i*step` 与末拍 `Last` 做相同的断言。
- 6 个用例分别覆盖：普通单次读（1）、FIFO 多包缓冲与不丢数据（2）、纯超时周期性读取（3）、禁用时忽略触发并冻结超时（4）、触发过快下的背压与数据完整（5）、单寄存器读与 1 字包的 `Last`（6）；每个用例的「RegTable 地址 ↔ `p_spi` 回送数据 ↔ `CheckResults` 期望」通过同一下标 `i` 闭环。
- 双模式回归由 [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L53-L55) 的 `create_tb_run` + `tb_run_add_arguments "-gOutputType_g=AXIS" "-gOutputType_g=AXIMM"` 实现，让同一 `top_tb` 跑两遍；BFM 与比较函数全部来自 `psi_tb` 库（`psi_tb_axi_pkg`/`psi_tb_compare_pkg`/`psi_tb_activity_pkg`/`psi_tb_txt_util`）。
- 失败即报错：所有期望/比较函数在不符时向 transcript 打印 `###ERROR###`，被 u1-l3 讲的 `run_check_errors "###ERROR###"` 捕获并让 CI 失败——所以这份测试台是 IP 行为正确性的「自动守门员」。

## 7. 下一步学习建议

- **u3-l3 参数化与 GUI 配置**：本讲 DUT 实例化时手填的 `MaxRegCount_g=16`、`MinBuffers_g=2`、`TimeoutUs_g=10`、`Output_g` 等参数，在真实交付时是通过 Vivado GUI 配置的。下一讲讲这些 generic 如何在 GUI 里暴露、如何映射到 RTL，与本讲的「`MinBuffers_g` 决定 FIFO 深度 → 影响背压用例」直接呼应。
- **u3-l5 二次开发实践：扩展该 IP**：当你给 IP 新增一个寄存器或一种行为（例如暴露 `DoneCnt` 为只读寄存器），本讲的 `top_tb` 是必须同步更新的地方之一——你需要为它新增一个用例（`StimCase`/`RespCase` 编号顺延到 7），并复用 `CheckResults` 或手写校验。u3-l5 会给出端到端的改动清单。
- **深读 `psi_tb` 库**：本讲只把 `axi_single_write`/`axi_expect_ar` 等当黑盒用。若想理解 BFM 内部如何把一次调用拆成 AR/W/R/B 通道的逐拍翻转（含 `arvalid` 等待 `arready` 的握手实现），可去 `psi_tb` 仓库读 `psi_tb_axi_pkg.vhd` 的过程体——这是写出更复杂自校验测试台的进阶基础。
- **回看 u2-l3 / u2-l4 / u2-l7**：本讲的用例 3（超时）、4（禁用）、5（背压）分别是这三讲 FSM、触发/超时、FIFO 存储机制的「行为对照实验」。若对某个用例的预期现象有疑问，回到对应的 RTL 讲义核对 FSM 状态与信号是最快的路径。
