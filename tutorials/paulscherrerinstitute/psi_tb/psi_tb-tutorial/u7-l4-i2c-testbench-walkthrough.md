# I2C 测试平台实战（psi_tb_i2c_pkg_tb）

## 1. 本讲目标

本讲是 I2C 单元（u7）的收尾实战。前面三讲（u7-l1 概览与初始化、u7-l2 主机事务、u7-l3 从机事务与时钟拉伸）已经把 `psi_tb_i2c_pkg` 里每一个公开过程讲透了，但它们都是「零件」。本讲要看的是「整车」——这些零件如何在一个真实可运行的 testbench 里组装起来，把 7 位/10 位寻址、读/写、ACK/NACK、Repeated Start、时钟拉伸这些场景一次性全覆盖。

学完本讲你应该能够：

- 说清为什么 master 与 slave 必须拆成两个**并发 process**、它们如何只靠共享的两根开漏线（`scl`/`sda`）完成「对拍」。
- 读懂 `psi_tb_i2c_pkg_tb.vhd` 的整体骨架，并能定位每一个测试场景对应的代码段。
- 掌握用 `print` 给长测试用例「分节」的可读性技巧。
- 在这个 TB 上安全地**扩展自己的 I2C 用例**——尤其是同时改 master 和 slave 两处、保证不发生错位。

## 2. 前置知识

本讲默认你已经学完 u7-l1 / u7-l2 / u7-l3。下面这些概念会直接用到，先做个一句话回顾：

- **开漏总线与上拉建模**：I2C 的 SCL/SDA 是开漏线，器件拉低驱动 `'0'`、松手驱动 `'Z'`，上拉电阻常驻驱动 `'H'`。`std_logic` 的多驱动解析能让 `'0'` 盖过 `'H'`，从而近似真实电气行为（u7-l1）。
- **`I2cPullup` / `I2cBusFree` / `I2cSetFrequency`**：上拉、释放本进程驱动、设置位时序频率（写进 `shared variable FreqClk_v`，默认 100 kHz）三个初始化原语（u7-l1）。
- **「主机拥有时钟」**：主机侧用 `...InclClock` 位级原语自己产生 SCL 脉冲；从机侧用 `...ExclClock` 等主机打出的边沿（u7-l2、u7-l3）。
- **`ExpectedAck` 与 `AckOutput`**：方向相反的两个应答参数。写事务里主机用 `ExpectedAck` 校验从机应答，读事务里主机用 `AckOutput` 主动给出应答（u7-l2）。
- **`Timeout` / `ClkStretch`**：从机侧每个等电平都带超时（默认 1 ms，超时只打印 `###ERROR###` 不挂死）；`ClkStretch` 让从机把 SCL 钳低一段时间来建模时钟拉伸（u7-l3）。
- **统一错误前缀 `###ERROR###`**：所有自检失败都用此前缀打印，被 CI 的 `run_check_errors "###ERROR###"` 抓取（u1-l3、u7-l2）。

如果你对上面任何一条感到陌生，建议先回到对应讲义复习，再读本讲。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 角色 |
| --- | --- |
| `testbench/psi_tb_i2c_pkg_tb.vhd` | **主战场**。一个空端口的 testbench，内含 `I2cPullup` 实例化与 `p_master` / `p_slave` 两个并发 process。 |
| `hdl/psi_tb_i2c_pkg.vhd` | **被测对象**。提供所有 `I2cMaster*` / `I2cSlave*` 过程的声明与实现，本讲在需要时回溯到这里的实现细节。 |

另外，这个 TB 是整个仓库当前**唯一**注册进 CI 的 testbench。在 [sim/config.tcl:36-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L36-L42) 中可以看到它被 `add_sources ... -tag tb` 收录，并用 `create_tb_run "psi_tb_i2c_pkg_tb"` / `add_tb_run` 注册为一次仿真运行。也就是说，跑 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）时，跑的就是它；它打印的任何 `###ERROR###` 都会直接让 CI 失败。这一点决定了本讲所有「跑通后确认无 `###ERROR###`」的实践都可在同一条流水线上验证。

## 4. 核心概念与源码讲解

本讲把 TB 拆成四个最小模块，外加一个贯穿全篇的核心认知（双 process 对拍）。每个模块先讲直觉，再上源码，最后给一个小实践。

### 4.1 双 process 对拍与 I2cPullup 实例化

#### 4.1.1 概念说明

真实的 I2C 系统里，master 和 slave 是两颗独立的芯片，它们之间**没有任何私有连线**，唯一的通信媒介就是 SCL/SDA 两根开漏线（再加上公共的地）。psi_tb 的 TB 忠实地复刻了这一点：

- master 的所有行为放进一个 `p_master` process；
- slave 的所有行为放进另一个 `p_slave` process；
- 两个 process **共享同一对 `scl` / `sda` 信号**，再无其它耦合。

这就是「**对拍**」：master 发 START，slave 就 WaitStart；master 发地址 `0x12` 读，slave 就 ExpectAddr `0x12` 读。两边各自独立推进，靠协议握手本身（等 SCL 上升沿、等 SDA 下降沿）在时间轴上自然对齐。这种结构的好处是——你写 TB 的方式和你心里想「master 这一步干什么、slave 这一步干什么」完全一致，没有隐藏的全局状态。

把上拉「焊」到总线上靠的是 `I2cPullup`。它是一个**并发过程调用**（concurrent procedure call），直接写在 architecture 的并发语句区，等价于一个常驻进程，持续把 `scl`/`sda` 往 `'H'` 拉。没有它，器件松手驱动 `'Z'` 后总线会悬空成 `'Z'` 而非 `'H'`，所有「判高要同时认 `'1'` 和 `'H'`」的逻辑就失去意义了。

#### 4.1.2 核心流程

TB 顶层的组装只有三件事，顺序如下：

```text
architecture sim
  ├─ 声明 signal scl : std_logic := 'H'
  ├─ 声明 signal sda : std_logic := 'H'
  ├─ I2cPullup(scl, sda)        ← 并发语句，常驻上拉
  ├─ p_master : process          ← 并发语句，主机剧本
  └─ p_slave  : process          ← 并发语句，从机剧本
```

两个信号的初值都设成 `'H'`，这样在仿真 `time = 0`、`I2cPullup` 还没来得及驱动的瞬间，总线也已经是高电平，避免 master 第一个 `LevelCheck('1', Scl, ...)` 误报。`I2cPullup` 的实现极简，就是把两根线持续赋成 `'H'`：

```vhdl
procedure I2cPullup(signal Scl : inout std_logic;
                    signal Sda : inout std_logic) is
begin
    Scl <= 'H';
    Sda <= 'H';
end procedure;
```

#### 4.1.3 源码精读

TB 的顶层骨架与 `I2cPullup` 实例化在 [testbench/psi_tb_i2c_pkg_tb.vhd:27-36](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L27-L36)：

```vhdl
architecture sim of psi_tb_i2c_pkg_tb is
    signal scl 	: std_logic := 'H';
    signal sda	: std_logic := 'H';
    
begin
    -- Pullup resistors
    I2cPullup(scl, sda);
```

- 第 28–29 行声明两根 `inout` 性质的 `std_logic` 信号，初值 `'H'`，对应物理上的「上拉到高」。
- 第 33 行 `I2cPullup(scl, sda);` 是一句**并发过程调用**，和下面的 `p_master`、`p_slave` 平级，三者同时在 `time = 0` 启动。

`I2cPullup` 的实现见 [hdl/psi_tb_i2c_pkg.vhd:399-404](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L399-L404)，注意它没有 `wait`，所以作为并发过程调用时，赋值语句会被综合器/仿真器视为对信号的持续驱动（等效于 `scl <= 'H';` 的并发赋值），从而与 master/slave 的 `'0'`/`'Z'` 驱动一起进入多驱动解析。

> **关键认知**：master、slave、pullup 三个并发体都往同一对信号写值，`std_logic` 的解析表保证 `'0'`（强低）盖过 `'H'`（弱高），任何一方拉低总线就呈现低，全部松手才回高——这正是开漏总线的语义。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：直观感受「三个并发体共享两根线」的解析行为。
2. **操作步骤**：
   - 打开 [testbench/psi_tb_i2c_pkg_tb.vhd:28-33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L28-L33)，确认 `scl`/`sda` 初值是 `'H'` 而不是 `'Z'`。
   - 在脑中（或纸面上）推演 `time = 0` 的瞬间：`I2cPullup` 驱 `'H'`，`p_master` 与 `p_slave` 的第一句都是 `I2cBusFree`（驱 `'Z'`），三者解析结果是什么？
3. **需要观察的现象**：仿真启动后波形上 `scl`/`sda` 应为稳定的高电平（`'H'`/`'1'`），直到 master 发出第一个 START 把 `sda` 拉低。
4. **预期结果**：总线在 START 之前一直是高；若把 `I2cPullup` 那行注释掉，`'Z'` 与 `'Z'` 解析仍为 `'Z'`，master 的 `LevelCheck('1', Sda, ...)` 会因为 `'Z' /= '1'` 且 `'Z' /= 'H'` 而立刻打印 `###ERROR###`。
5. 结论：`I2cPullup` 不是装饰，而是开漏模型能工作的物理前提。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `I2cPullup(scl, sda)` 从并发语句区移进 `p_master` 进程的开头，会发生什么？

> **答案**：`I2cPullup` 内部没有 `wait`，移进进程后，进程执行完 `Scl <= 'H'; Sda <= 'H';` 就**永久挂起**（进程没有更多语句、也没 `wait`，仿真器会报「process without wait」错误或在 0 时刻无限循环）。所以上拉必须以**并发过程调用**的形式常驻，不能塞进普通进程。

**练习 2**：为什么 `scl`/`sda` 的初值要写成 `'H'` 而不是 `'1'`？

> **答案**：`'H'` 是弱高，模拟上拉电阻；这样器件驱动的强 `'0'` 能正确盖过它。如果初值写成 `'1'`（强高），在 `I2cPullup` 还没生效的瞬间，总线会呈现强高而非弱高，与真实开漏语义不符；逻辑上虽不一定立刻报错，但破坏了「上拉是弱驱动」的建模一致性。

---

### 4.2 print 分节标记：长用例的可读性管理

#### 4.2.1 概念说明

这个 TB 有十几条用例，跑一次仿真会往 Transcript 里打几百行消息。如果所有用例平铺，出问题时几乎不可能一眼定位「现在跑到哪了」。psi_tb 用了一个极简却有效的约定来治理这种复杂度：

- **章节标题**用 `print(">> 标题")` 打印，前缀 `>>` 让人眼/脚本都能快速识别这是一个分节边界；
- **每条用例**在标题之后再打一行 `print("用例描述")`（不带 `>>`），说明这一段要验证什么。

注意：**只有 master 进程打 print**，slave 进程一句 print 都没有。因为两个进程并发执行，如果两边都打 section 标题，Transcript 里的文字会交错穿插反而更乱；让 master 当「叙述者」、slave 当「静默对演员」，输出就保持线性可读。

`print` 本身来自 `psi_tb_txt_util`（u2-l1 讲过），底层就是 `write` + `writeline(output, ...)`，把字符串送到 Transcript。

#### 4.2.2 核心流程

master 进程的剧本被四个章节标记切成四大块：

```text
print(">> Addressing")                       ← 5 条寻址用例
print(">> Data Transfers")                   ← 6 条数据传输用例
print(">> Repeated Start (Mixed Transfer)")  ← 1 条混合读写用例
print(">> Clock Stretching")                 ← 2 条含时钟拉伸的用例
```

每条用例内部一律遵循同一个节奏：

```text
print("用例的一句话描述")
I2cMasterSendStart(...)
I2cMasterSendAddr(...)
... 数据/应答 ...
I2cMasterSendStop(...)
wait for 10 us;        ← 用例之间的「呼吸」，方便在波形上分隔
```

#### 4.2.3 源码精读

四个章节标记分别见：

- [testbench/psi_tb_i2c_pkg_tb.vhd:44](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L44)：`print(">> Addressing");`
- [testbench/psi_tb_i2c_pkg_tb.vhd:82](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L82)：`print(">> Data Transfers");`
- [testbench/psi_tb_i2c_pkg_tb.vhd:135](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L135)：`print(">> Repeated Start (Mixed Transfer)");`
- [testbench/psi_tb_i2c_pkg_tb.vhd:149](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L149)：`print(">> Clock Stretching");`

以「Data Transfers」里的「Single Byte Write, ACK」为例，看一条用例的完整叙述结构 [testbench/psi_tb_i2c_pkg_tb.vhd:109-115](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L109-L115)：

```vhdl
-- Single Byte Write, ACK
print("Single Byte Write, ACK");
I2cMasterSendStart(scl, sda, "M: start");
I2cMasterSendAddr(16#13#, false, scl, sda, "M: address", 7);
I2cMasterSendByte(16#67#, scl, sda, "M: data-write");
I2cMasterSendStop(scl, sda, "M: stop");
wait for 10 us;
```

注意每个 BFM 调用的 `Msg` 参数都以 `"M: "` 开头——这是另一层非正式约定：master 侧消息带 `M:`、slave 侧消息带 `S:`。这样一旦某条 `###ERROR###` 冲进 Transcript，看前缀就能立刻判断是哪一方报的、对应哪一条用例的哪一个动作。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：体会 `print` 分节在长 Transcript 中的导航价值。
2. **操作步骤**：
   - 在仓库根目录跑一次仿真（参考 u1-l3）：例如 `vsim -do sim/run.tcl`，或用 GHDL 的 `sim/runGhdl.tcl`。
   - 仿真结束后打开 `sim/Transcript.transcript`（或 ModelSim 的 Transcript 窗口）。
   - 用搜索功能定位 `>>` 出现的四处。
3. **需要观察的现象**：每处 `>>` 之前应有上一条用例末尾的 `wait for 10 us` 间隔；每处 `>>` 之后紧跟若干 `M:` / `S:` 开头的自检消息（正常情况下无 `###ERROR###`）。
4. **预期结果**：Transcript 呈现清晰的「四段式」结构，与源码的四个章节一一对应；最后能看到 `SIMULATIONS COMPLETED SUCCESSFULLY`（详见 u1-l3）。
5. 若本机暂无仿真器，可改为纯阅读：对照本节列出的四个行号，在源码里把这四段边界标注出来即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 slave 进程里完全不写 `print`？

> **答案**：两个进程并发执行，若都打 print，Transcript 输出会交错、章节边界被打乱；让 master 单方面承担「叙述」职责、slave 保持静默，能保证输出线性、可读。slave 的状态变化仍可通过它驱动的 `scl`/`sda` 波形观察，不依赖 print。

**练习 2**：`print(">> Clock Stretching")` 这一行在 master 里出现在第 149 行，对应 slave 里的「时钟拉伸」用例却没有任何 print，那 slave 怎么知道现在该跑时钟拉伸场景了？

> **答案**：slave 不靠 print、也不靠任何「场景编号」来同步，它完全靠**协议握手**。slave 进程只是顺序地 WaitStart→ExpectAddr→…，master 发什么、slave 就握手什么。时钟拉伸是 slave 在 `ExpectAddr`/`ExpectByte` 里通过 `ClkStretch` 参数主动钳低 SCL 实现的，与 master 是否打 print 无关。

---

### 4.3 p_master process：主机侧场景全集

#### 4.3.1 概念说明

`p_master` 是 master 的「剧本」，它把 u7-l2 里学过的所有主机过程按真实 I2C 事务的顺序串起来。它的存在证明了 psi_tb 的一个设计取向：**BFM 过程是无状态的、一次调用等于一次完整的协议动作**。所以写 master 剧本就像写「要干什么」的清单——发 START、发地址、收/发字节、发 STOP，每一步都是一行过程调用。

master 剧本要覆盖的场景全集如下，每一条都对应一个公开的协议特性：

| 章节 | 用例 | 覆盖的特性 |
| --- | --- | --- |
| Addressing | 7b/10b × 读/写 × ACK/NACK | 7 位与 10 位寻址、R/W 位、应答校验三态 |
| Data Transfers | 单字节读 ACK/NACK、双字节读、单字节写 ACK/NACK、双字节写 | 读/写、多字节、末字节 NACK 惯例 |
| Repeated Start | 写 1 字节后 Repeated Start 读 1 字节 | 不发 STOP 直接转向、混合读写 |
| Clock Stretching | 单字节读、单字节写 | 从机钳时钟时主机仍能正常完成（由对拍进程配合） |

#### 4.3.2 核心流程

master 进程的整体节奏：

```text
setup:  I2cBusFree → I2cSetFrequency(400 kHz) → wait 1 us
for 每个章节:
    print(">> 章节")
    for 每条用例:
        print("用例描述")
        I2cMasterSendStart(...)
        I2cMasterSendAddr(... AddrBits, ExpectedAck ...)
        [I2cMasterSendByte | I2cMasterExpectByte] ...   (可能多字节)
        I2cMasterSendStop(...)
        wait for 10 us
wait;   ← 进程末尾的永久挂起
```

几个值得记住的约定：

1. **频率只在 master 设一次**：`I2cSetFrequency(400.0e3)` 写的是包体内的 `shared variable FreqClk_v`（默认 100 kHz），slave 进程共享同一个变量，故不必再设。400 kHz 是 I2C Fast Mode。
2. **末字节 NACK 惯例**：在多字节**读**里，最后一字节用 `I2cMasterExpectByte(..., '1')`（`AckOutput='1'` 即 NACK）——这是 I2C 规范建议的「主机读最后字节时发 NACK 告知 slave 别再发」。
3. **每个 BFM 调用都带 `Msg`**：失败时这串文字会拼进 `###ERROR###: - <Func> - <General> - <User>` 消息（`GenMessage`，见 u7-l2），是排查的第一线索。

#### 4.3.3 源码精读

进程头与初始化见 [testbench/psi_tb_i2c_pkg_tb.vhd:36-41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L36-L41)：

```vhdl
p_master : process
begin
    -- Setup
    I2cBusFree(scl, sda);
    I2cSetFrequency(400.0e3);
    wait for 1 us;
```

- `I2cBusFree` 先把本进程对 `scl`/`sda` 的驱动松手成 `'Z'`，避免与上拉或对端冲突（u7-l1）。
- `I2cSetFrequency(400.0e3)` 设 400 kHz，于是位时序由包内 `ClkQuartPeriod` 等函数实时算出：周期 \(T = 1/f\)，四分之一周期为

\[
T_{q} = \frac{1}{4f} = \frac{1}{4 \times 400\,000\,\text{Hz}} = 625\,\text{ns}
\]

半周期 \(T_h = 1.25\,\mu\text{s}\)。这正是 `SendBitInclClock` 里「¼T 建立 + ½T SCL 高 + ¼T 回低」的时间基准（u7-l2）。
- `wait for 1 us` 给 slave 进程一点时间也完成它开头的 `I2cBusFree`，两边都松手后再开始。

寻址章节挑两条对照看。7 位读 ACK [testbench/psi_tb_i2c_pkg_tb.vhd:47-51](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L47-L51)：

```vhdl
print("Do 7b address cycle without data, ACK, read");
I2cMasterSendStart(scl, sda, "M: 7b start");
I2cMasterSendAddr(16#12#, true, scl, sda, "M: 7b address", 7);
I2cMasterSendStop(scl, sda, "M: 7b stop");
```

`I2cMasterSendAddr` 第 2 个参数 `true` 表示读、第 6 个参数 `7` 表示 7 位寻址、`ExpectedAck` 取默认 `'0'`（期望从机 ACK）。10 位读 NACK 则在尾部显式加 `'1'` [testbench/psi_tb_i2c_pkg_tb.vhd:77](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L77)：

```vhdl
I2cMasterSendAddr(16#112#, true, scl, sda, "M: 7b address", 10, '1');
```

（注意这条的 `Msg` 文案写成了 `"M: 7b address"`，虽是 10 位用例但文案未改——属于源码里一处无害的笔误，不影响功能。）

数据传输章节里，「双字节读」最能体现末字节 NACK 惯例 [testbench/psi_tb_i2c_pkg_tb.vhd:101-107](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L101-L107)：

```vhdl
I2cMasterExpectByte(16#AB#, scl, sda, "M: data-read");            -- 末参数默认 '0' → 主机回 ACK
I2cMasterExpectByte(16#CD#, scl, sda, "M: data-read", '1');       -- 显式 '1' → 主机回 NACK
```

「Repeated Start（混合读写）」展示了一次事务里写转读的完整链路 [testbench/psi_tb_i2c_pkg_tb.vhd:138-146](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L138-L146)：

```vhdl
I2cMasterSendStart(scl, sda, "M: start");
I2cMasterSendAddr(16#13#, false, scl, sda, "M: address", 7);   -- 写
I2cMasterSendByte(16#67#, scl, sda, "M: data-write");
I2cMasterSendRepeatedStart(scl, sda, "M: start");              -- 不发 STOP，直接 Repeated Start
I2cMasterSendAddr(16#13#, true, scl, sda, "M: address", 7);    -- 切成读
I2cMasterExpectByte(16#89#, scl, sda, "M: data-read", '1');
I2cMasterSendStop(scl, sda, "M: stop");
```

进程末尾是 [testbench/psi_tb_i2c_pkg_tb.vhd:166](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L166) 的 `wait;`，让 master 跑完所有用例后永久挂起，把总线让给仿真结束。

#### 4.3.4 代码实践（阅读 + 行为预测）

1. **实践目标**：在不跑仿真的前提下，能根据 master 剧本预测 slave 剧本必须怎么对拍。
2. **操作步骤**：
   - 读 [testbench/psi_tb_i2c_pkg_tb.vhd:109-115](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L109-L115)（「Single Byte Write, ACK」的 master 侧）。
   - 在脑中写出 slave 侧应有的对拍序列：先 `I2cSlaveWaitStart`，再 `I2cSlaveExpectAddr(16#13#, false, ...)`，再 `I2cSlaveExpectByte(16#67#, ...)`，最后 `I2cSlaveWaitStop`。
   - 翻到 [testbench/psi_tb_i2c_pkg_tb.vhd:224-227](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L224-L227) 核对，应与你的预测完全一致。
3. **需要观察的现象**：master 写 `16#67#`，slave 也 ExpectByte `16#67#`；master 的 `ExpectedAck` 默认 `'0'`，slave 的 `AckOutput` 也默认 `'0'`，两边对 ACK 的预期一致。
4. **预期结果**：两侧参数一一对应，无 `###ERROR###`。
5. 若你把任一侧的 `16#67#` 改成别的值（只是脑中推演，不必真改源码），会怎样？→ slave 的 `ExpectByte` 在回读 SDA 时发现数据不符，打印 `###ERROR###: - I2cSlaveExpectByte - Received wrong data [...]`（u7-l3）。

#### 4.3.5 小练习与答案

**练习 1**：master 在 `I2cSetFrequency(400.0e3)` 之后为什么还要 `wait for 1 us`？能不能去掉？

> **答案**：这一小段等待主要是给两个并发进程一个稳定的「同步窗口」，确保 master 和 slave 都完成了各自的 `I2cBusFree`、总线稳定在高电平后，再开始第一个 START。去掉它通常仍能跑通（因为 START 本身也会等电平），但保留它让波形更干净、降低仿真初期因进程调度顺序带来的边界风险。

**练习 2**：master 在「双字节读」里对第二字节传了 `'1'`（NACK）。这个 `'1` 绑定到 `I2cMasterExpectByte` 的哪个形参？它的语义是「期望从机给 NACK」吗？

> **答案**：绑定到 `AckOutput`（不是 `ExpectedAck`）。在**读**事务里，主机是数据的接收方，由主机给出应答，所以这个参数是「主机要驱动到 SDA 上的应答值」，`'1'` 表示主机发 NACK。它不是「期望」，而是「主机主动输出的动作」。读末字节发 NACK 是 I2C 惯例，告知 slave 不要再继续发下一字节。（写事务里方向反过来，见 u7-l2 的 `ExpectedAck`。）

---

### 4.4 p_slave process：对拍与时钟拉伸场景

#### 4.4.1 概念说明

`p_slave` 是 slave 的「剧本」，结构与 `p_master` **逐条对应**：master 的每一条用例，在 slave 里都有一条对拍段。这是本讲最重要的实践纪律——

> **铁律：在 master 加一条用例，就必须在 slave 的对应位置加一条对拍用例；反之亦然。**

原因在于「对拍」靠的是协议握手，而握手要求两侧**在同一时间窗口内扮演互补角色**。如果你只在 master 加了「写 3 字节」，slave 那边却还停在原来的序列，master 发出的字节就没人 `ExpectByte`，总线会卡在某个等电平的 `LevelWait` 上直到 1 ms 超时，然后打印一堆 `###ERROR###`，后续所有用例全部雪崩错位。

slave 剧本唯一比 master 多的能力，是「**时钟拉伸**」——在 `ExpectAddr`/`ExpectByte`/`SendByte` 上传 `ClkStretch` 参数，让从机在位间隙把 SCL 钳低一段时间，测试主机侧的容忍能力（u7-l3）。这一节的两个用例就是专门验证它的。

#### 4.4.2 核心流程

slave 进程的骨架与 master 同构：

```text
setup:  I2cBusFree   ← 注意：slave 不调 I2cSetFrequency，共享 master 设的值
for 每条对拍用例:
    I2cSlaveWaitStart(...)
    I2cSlaveExpectAddr(... AckOutput, Timeout, ClkStretch ...)
    [I2cSlaveExpectByte | I2cSlaveSendByte] ...   (可能多字节)
    I2cSlaveWaitStop(...)
wait;
```

时钟拉伸场景里，slave 给 `ExpectAddr`/`ExpectByte` 显式传 `Timeout` 与 `ClkStretch`：

```text
-- 例如单字节读（含时钟拉伸）：
I2cSlaveExpectAddr(16#13#, true, scl, sda, "...", 7, '0', 1 ms, 10 us);
I2cSlaveSendByte (16#AB#,        scl, sda, "...",    '0', 1 ms, 10 us);
                  ↑AckOutput            ↑Timeout ↑ClkStretch
```

`ClkStretch = 10 us` 意味着从机在每位 SCL 低电平期间额外把 SCL 钳低 10 µs；主机侧的 `SendBitInclClock` 用 `LevelWait('1', Scl, 1 ms, ...)` 等待 SCL 真的升高（u7-l2、u7-l3），所以只要 `ClkStretch` 小于主机侧 1 ms 的硬上限，主机就能正确容忍。

#### 4.4.3 源码精读

slave 进程头与初始化 [testbench/psi_tb_i2c_pkg_tb.vhd:171-174](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L171-L174)：

```vhdl
p_slave : process
begin
    -- setup		
    I2cBusFree(scl, sda);
```

注意 slave **没有** `I2cSetFrequency`——因为 `FreqClk_v` 是 `shared variable`（[hdl/psi_tb_i2c_pkg.vhd:162](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L162)），master 设过之后 slave 直接共享。两个进程都用同一个 `ClkQuartPeriod`/`ClkHalfPeriod`，位时序才能严丝合缝对上。

「Single Byte Write, ACK」的 slave 对拍段 [testbench/psi_tb_i2c_pkg_tb.vhd:224-227](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L224-L227)，与 4.3.3 的 master 段逐行镜像：

```vhdl
-- Single Byte Write, ACK
I2cSlaveWaitStart(scl, sda, "S: wait start");
I2cSlaveExpectAddr(16#13#, false, scl, sda, "S: check address", 7);
I2cSlaveExpectByte(16#67#, scl, sda, "S: data-write");
I2cSlaveWaitStop(scl, sda, "S: wait stop");
```

`I2cSlaveExpectByte` 的实现见 [hdl/psi_tb_i2c_pkg.vhd:689-707](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L689-L707)：它先用 `ExpectByteExclClock` 逐位 `CheckBitExclClock` 校验主机发来的 8 位数据，再用 `SendBitExclClock(AckOutput, ...)` 驱动应答位，最后 `I2cBusFree` 松手。默认 `AckOutput='0'` 即返回 ACK。

时钟拉伸的两条用例 [testbench/psi_tb_i2c_pkg_tb.vhd:254-265](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L254-L265)：

```vhdl
-- Single Byte Read （含时钟拉伸）
I2cSlaveWaitStart(scl, sda, "S: wait start");
I2cSlaveExpectAddr(16#13#, true, scl, sda, "S: check address", 7, '0', 1 ms, 10 us);
I2cSlaveSendByte (16#AB#,        scl, sda, "S: data-read",     '0', 1 ms, 10 us);
I2cSlaveWaitStop(scl, sda, "S: wait stop");	

-- Single Byte Write（含时钟拉伸）
I2cSlaveWaitStart(scl, sda, "S: wait start");
I2cSlaveExpectAddr(16#13#, false, scl, sda, "S: check address", 7, '0', 1 ms, 5 us);
I2cSlaveExpectByte(16#67#,        scl, sda, "S: data-write",    '0', 1 ms, 7 us);
I2cSlaveWaitStop(scl, sda, "S: wait stop");
```

读用例用 `ClkStretch = 10 us`、写用例用 `5 us` / `7 us`——刻意取不同值，说明 `ClkStretch` 可以「逐调用」定制，并非全局固定。对应的 master 时钟拉伸用例 [testbench/psi_tb_i2c_pkg_tb.vhd:151-164](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L151-L164) 则是**普通调用、不带任何特殊参数**，因为主机的 `LevelWait('1', Scl, 1 ms, ...)` 本就容忍从机任意钳低（只要不超过 1 ms）——这正是 u7-l3 强调的「主机拥有时钟，但容忍拉伸」。

进程末尾同样是 [testbench/psi_tb_i2c_pkg_tb.vhd:267](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L267) 的 `wait;`。

#### 4.4.4 代码实践（综合，含动手改 TB）

> ⚠️ 本实践要求修改 `testbench/psi_tb_i2c_pkg_tb.vhd`。这是讲义里给读者的练习，请在一个**学习用副本**上操作；若直接改仓库源码，记得事后用 `git checkout` 还原，不要把练习改动提交。

1. **实践目标**：在 TB 中新增一个用例——主机连续写 3 个字节，从机逐一 `ExpectByte` 并返回 ACK，跑通后确认 Transcript 中无 `###ERROR###`。
2. **操作步骤**：
   - **选插入点**：在 master 的「Two Byte Write」之后插入（即 [testbench/psi_tb_i2c_pkg_tb.vhd:132](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L132) 的 `wait for 10 us;` 之后）；**slave 侧必须在同一逻辑位置**插入（[testbench/psi_tb_i2c_pkg_tb.vhd:240](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L240) 的 `I2cSlaveWaitStop(...)` 之后）。两处缺一不可，否则错位。
   - **在 master 加**（示例代码，标注为「示例代码」）：

     ```vhdl
     -- Three Byte Write, all ACK （示例代码：本练习新增）
     print("Three Byte Write, ACK");
     I2cMasterSendStart(scl, sda, "M: start");
     I2cMasterSendAddr (16#13#, false, scl, sda, "M: address", 7);
     I2cMasterSendByte (16#11#, scl, sda, "M: data-write");   -- ExpectedAck 默认 '0'，期望 ACK
     I2cMasterSendByte (16#22#, scl, sda, "M: data-write");   -- 同上
     I2cMasterSendByte (16#33#, scl, sda, "M: data-write");   -- 同上：三字节全要 ACK
     I2cMasterSendStop (scl, sda, "M: stop");
     wait for 10 us;
     ```
   - **在 slave 加对应对拍**（示例代码）：

     ```vhdl
     -- Three Byte Write, all ACK （示例代码：本练习新增，与 master 对拍）
     I2cSlaveWaitStart  (scl, sda, "S: wait start");
     I2cSlaveExpectAddr (16#13#, false, scl, sda, "S: check address", 7);
     I2cSlaveExpectByte (16#11#, scl, sda, "S: data-write");   -- AckOutput 默认 '0'，返回 ACK
     I2cSlaveExpectByte (16#22#, scl, sda, "S: data-write");
     I2cSlaveExpectByte (16#33#, scl, sda, "S: data-write");
     I2cSlaveWaitStop   (scl, sda, "S: wait stop");
     ```
   - **跑仿真**：用 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）执行。
3. **需要观察的现象**：
   - Transcript 出现新增的 `Three Byte Write, ACK` 分节；
   - 该用例下三字节的 `M: data-write` / `S: data-write` 消息正常出现，且**没有任何** `###ERROR###`；
   - 后续的「Repeated Start」「Clock Stretching」用例依旧正常完成（说明没有错位）。
4. **预期结果**：仿真以 `SIMULATIONS COMPLETED SUCCESSFULLY` 结束（u1-l3），`run_check_errors "###ERROR###"` 扫描无命中，CI 绿。
5. **若失败如何排查**：
   - 只改了 master 没改 slave（或反之）→ 会在新增段立刻出现 `###ERROR###` 并向后雪崩，回到「两处都改且对齐」即可。
   - master 与 slave 的数据值不一致（如一边 `16#22#`、一边 `16#23#`）→ slave 的 `I2cSlaveExpectByte` 打印 `Received wrong data`，对照修改。
   - 想验证 ACK 路径：把 slave 第三字节的 `AckOutput` 改成 `'1'`（NACK），同时把 master 第三字节的 `ExpectedAck` 也改成 `'1'`，应依旧无错；只改一边则会报 ACK 不符。
6. **待本地验证**：不同仿真器（ModelSim vs GHDL）的 Transcript 文案与行号细节可能略有差异，以上为基于源码的预期；具体输出请以本机实测为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 slave 进程不调 `I2cSetFrequency`？如果删掉 master 里的 `I2cSetFrequency(400.0e3)`，仿真的位时序会变成什么？

> **答案**：`FreqClk_v` 是包体内的 `shared variable`，两个进程共享，master 设一次即可，slave 无需重复设置。若删掉 master 的设置，`FreqClk_v` 保持初始值 `100.0e3`（[hdl/psi_tb_i2c_pkg.vhd:162](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L162)），位时序退化为 100 kHz（Standard Mode），周期从 2.5 µs 变成 10 µs，仿真更慢但功能仍正确——因为两侧用的是同一个频率。

**练习 2**：时钟拉伸用例里，为什么 master 端的调用看上去和普通读写一模一样、没有任何「拉伸」参数？

> **答案**：时钟拉伸是**从机**主动施加的行为（钳低 SCL），主机只是「被拉伸」的一方。主机侧的位级原语 `SendBitInclClock`/`CheckBitInclClock` 在驱动 SCL 升高后用 `LevelWait('1', Scl, 1 ms, ...)` 等待 SCL 真的变高（[hdl/psi_tb_i2c_pkg.vhd:257](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L257)），因此天然容忍从机把 SCL 多钳低一会儿。所以「拉伸」只体现在 slave 的 `ClkStretch` 参数上，master 端无需感知。

**练习 3**：如果把 slave 时钟拉伸用例里的 `ClkStretch` 从 `10 us` 调成 `2 ms`，会发生什么？

> **答案**：会触发主机侧的 1 ms 超时。`SendBitInclClock` 等待 SCL 升高的上限是硬编码的 `1 ms`（u7-l3），`ClkStretch = 2 ms` 超过此上限，主机会打印 `###ERROR###: ... SCL held low by other device ...`。这也解释了 u7-l3 的告诫：`ClkStretch` 必须小于主机侧 1 ms 的上限。

## 5. 综合实践

把本讲四块知识串成一个完整任务：**为 TB 新增一个「读 3 字节」用例，并在其中复用本讲的所有约定**。

要求：

1. 在 master 的「Two Byte Read」之后新增一段：主机发起 START、7 位寻址读、连续 `ExpectByte` 3 个字节（数据自选，如 `16#01#`/`16#02#`/`16#03#`），末字节按 I2C 惯例发 NACK，最后 STOP。
2. 在 slave 的对应位置新增对拍段：`WaitStart` → `ExpectAddr(读)` → `SendByte` × 3（前两字节 `AckOutput` 默认 `'0'`/ACK 的对端校验、末字节配合 master 的 NACK）。
3. 给新增用例加一行 `print("Three Byte Read")`（不带 `>>`，作为 Data Transfers 章节下的一条子用例）。
4. 跑 `sim/run.tcl` 或 `sim/runGhdl.tcl`，确认：
   - 新用例段无 `###ERROR###`；
   - 末字节 master 发 NACK、slave 用 `I2cSlaveSendByte` 末参数配合（注意 `I2cSlaveSendByte` 的末字节应答由 master 给出，slave 侧是 `ExpectedAck`——回顾 u7-l2/u7-l3 的方向约定，确认你传对了参数）；
   - 仿真仍以 `SIMULATIONS COMPLETED SUCCESSFULLY` 结束。

提示：

- 「读」方向上应答由**主机**给出，所以末字节 NACK 体现在 master 的 `I2cMasterExpectByte(..., '1')`（`AckOutput`）；slave 端 `I2cSlaveSendByte` 末字节可用默认 `ExpectedAck='0'` 校验主机给了 ACK、或显式 `'1'` 校验给了 NACK——按你的设计选一种并保持两侧一致。
- 严格遵守 4.4 的「铁律」：master 与 slave 必须在同一逻辑位置同时新增、用例数量一致。
- 完成后用 `grep '###ERROR###' sim/Transcript.transcript`（或仿真器等价操作）确认零命中。

## 6. 本讲小结

- psi_tb 的 I2C TB 用 **`I2cPullup` 并发过程调用 + 两个并发 process（`p_master`/`p_slave`）** 忠实建模了真实 I2C：三个并发体共享同一对开漏线，靠 `std_logic` 多驱动解析呈现「强低盖弱高」的电气语义。
- **对拍**是核心组织方式：master 的每条用例在 slave 里都有一条逐行镜像的对拍段，两侧只靠协议握手（等 SCL/SDA 边沿）在时间轴上对齐，没有隐藏全局状态。
- **print 分节**用 `>>` 前缀做章节标题、普通 `print` 做子用例描述，且只在 master 单方面叙述，让长 Transcript 保持线性可读；`M:`/`S:` 消息前缀进一步标注来源。
- master 剧本覆盖 7b/10b 寻址、读/写、ACK/NACK、Repeated Start、末字节 NACK 惯例；频率只在 master 设一次（`shared variable` 共享）。
- slave 剧本唯一多出的能力是 **`ClkStretch`**——逐调用定制、刻意取不同值（10/5/7 µs）验证主机对拉伸的容忍（上限 1 ms）。
- 扩展用例的铁律：**master 与 slave 必须同位置、同数量地一起改**，否则协议握手失配、后续用例雪崩错位。

## 7. 下一步学习建议

- I2C 单元到此完整闭环。建议回头用 `sim/run.tcl` 实跑一次本 TB，在波形上对照本讲的讲解观察 START/STOP/Repeated Start 的 SDA 翻转与 SCL 脉冲，把「文字描述」落实成「波形直觉」。
- 接下来进入 **u8-l1（仿真脚本与 CI 流程深入）** 和 **u8-l2（编码约定、错误消息机制与二次开发指南）**。u8-l2 会把本讲反复出现的 `###ERROR###` 前缀、`GenMessage` 消息拼接、`compare→activity→bfm` 复用链做系统性总结，并教你按 psi_tb 的既有约定**新增一个属于自己的 BFM 或检查过程**——相当于把本讲的「扩展用例」升级为「扩展库」。
- 若你正在进行真实的 I2C DUT 验证，可把本 TB 的 `p_slave` 段替换成你的 DUT（DUT 即从机），保留 `p_master` 作为驱动剧本，这就是把 psi_tb BFM 用到真实项目里的典型落地方式。
