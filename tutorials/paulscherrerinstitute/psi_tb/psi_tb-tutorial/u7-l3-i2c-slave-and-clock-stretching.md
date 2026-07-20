# I2C 从机事务与时钟拉伸

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `psi_tb_i2c_pkg` 中六个从机侧公开过程（`I2cSlaveWaitStart` / `WaitRepeatedStart` / `WaitStop` / `ExpectAddr` / `ExpectByte` / `SendByte`）各自做什么、按什么顺序调用。
- 理解「主机拥有时钟」这一命名约定在代码里的体现：从机过程全部走 `ExclClock`（不含时钟）位级原语，即**等主机打出 SCL 边沿**，而不是自己产生时钟。
- 解释 `Timeout` 参数如何让从机过程在主机「不来」时也能打印 `###ERROR###` 后继续，避免仿真永久挂死。
- 解释 `ClkStretch`（时钟拉伸）如何在仿真里建模「从机把 SCL 拉低一段时间」，并看清主机侧的 `LevelWait('1', Scl, 1 ms, "SCL held low by other device")` 是怎样配合等待的。
- 能够对照 `testbench/psi_tb_i2c_pkg_tb.vhd` 里 `p_slave` 进程，自己拼出一段带时钟拉伸的读取事务，并让它与 `p_master` 正确对拍。

本讲是 u7（I2C）单元的第三讲，承接 u7-l2（主机事务），是阅读 u7-l4（完整 testbench）的最后一块拼图。

## 2. 前置知识

在进入从机源码前，先用三小节把 u7-l1、u7-l2 已建立的概念与本讲的衔接点理顺。

### 2.1 开漏总线模型回顾

I2C 的 SCL/SDA 是开漏 + 上拉总线。psi_tb 用 `std_logic` 多驱动解析来近似它的物理行为（u7-l1 已详述）：

- 任何器件想把线拉低 → 驱动 `'0'`；
- 器件「松手」→ 驱动 `'Z'`；
- 上拉电阻常驻 → 驱动 `'H'`；
- 解析规则：强 `'0'` 盖过弱 `'H'`，所以「有人拉低就是低，所有人都松手才是高」。

判「高」时必须同时认 `'1'` 与 `'H'`，这是上拉模型能工作的前提。本讲的从机过程大量复用 `LevelCheck` / `LevelWait` 两个私有过程，它们内部就做了这种 `'H'` 兼容处理。

### 2.2 「主机拥有时钟」与 InclClock / ExclClock 命名

这是本讲最关键的一组概念。在 I2C 里，**SCL 永远由主机驱动**，从机只能「拉低它来拖延」（即时钟拉伸），但不能主动把它推高。psi_tb 把这一点直接编码进了过程命名：

- 主机侧位级原语叫 `SendBitInclClock` / `CheckBitInclClock`（**Incl**uding Clock，**含**时钟）：主机自己 `Scl <= 'Z'`（松手让上拉推高）产生上升沿、`Scl <= '0'` 产生下降沿，整套时钟节拍由主机打。
- 从机侧位级原语叫 `SendBitExclClock` / `CheckBitExclClock`（**Excl**uding Clock，**不含**时钟）：从机**不产生**时钟，而是用 `LevelWait` **等**主机打出的 SCL 上升沿和下降沿。

所以同样一位数据的传输：

| 角色 | 谁驱动 SCL | 谁驱动 SDA（数据位） | 用的位级原语 |
|------|-----------|---------------------|--------------|
| 主机发 1 位 | 主机 | 主机 | `SendBitInclClock` |
| 主机读 1 位 | 主机 | 从机 | `CheckBitInclClock`（主机侧）/ `SendBitExclClock`（从机侧） |
| 从机发 1 位 | 主机 | 从机 | `SendBitExclClock` |
| 从机读 1 位 | 主机 | 主机 | `CheckBitExclClock`（从机侧） |

记住一句话：**从机过程里所有的 SCL 动作都是「等」，不是「打」**。本讲后面看到 `LevelWait('1', Scl, ...)` 和 `LevelWait('0', Scl, ...)` 成对出现，就是这个意思。

### 2.3 一次事务在从机眼里长什么样

从从机进程的视角，一次完整 I2C 事务的调用骨架是：

```
I2cSlaveWaitStart(...)              -- 等 START
I2cSlaveExpectAddr(addr, rw, ...)   -- 收地址字节，回 ACK/NACK
-- 写事务：主机继续发数据
I2cSlaveExpectByte(data, ...)       -- 收 1 字节，回 ACK/NACK
-- 读事务：从机发数据
I2cSlaveSendByte(data, ...)         -- 发 1 字节，检查主机 ACK
I2cSlaveWaitStop(...)               -- 等 STOP
```

混合读写用 `I2cSlaveWaitRepeatedStart` 代替中间的 `WaitStop`+`WaitStart`。本讲的四个最小模块就是把这个骨架的每一段拆开讲。

### 2.4 两个工具过程：LevelCheck 与 LevelWait

它们是从机过程的「等待与断言」底座，定义在包体私有不分：

- `LevelCheck(Expected, Sig, ...)`：**不等待**，只断言当前 `Sig` 是否等于 `Expected`（`'1'` 时兼容 `'H'`），不等就打印 `###ERROR###`。
- `LevelWait(Expected, Sig, Timeout, ...)`：**带超时地等待** `Sig` 变成 `Expected`；超时则打印 `###ERROR###` 但**照样返回**（`severity error` 不中断仿真）。

`LevelWait` 的「超时也返回」是整个从机侧 `Timeout` 参数能防挂死的根因，第 4.4 节会精读。

## 3. 本讲源码地图

本讲只涉及一个源文件，外加一个 testbench 做实战参照：

| 文件 | 作用 |
|------|------|
| [hdl/psi_tb_i2c_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd) | I2C BFM 包，本讲精读其中的**从机侧过程**与它们依赖的私有位级原语 |
| [testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd) | 官方示例 TB，`p_slave` 进程是与 `p_master` 对拍的真实范例，含时钟拉伸用例 |

包内与本讲相关的代码点分布如下：

- 公开声明（包头）：`Slave Side Transactions` 注释块起的六个过程，[hdl/psi_tb_i2c_pkg.vhd:91-141](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L91-L141)。
- 私有位级原语：`SendBitExclClock` / `CheckBitExclClock`，是实现 `ClkStretch` 的真正所在。
- 公开从机过程实现：`I2cSlaveWaitStart` 起到 `I2cSlaveSendByte` 结束，是本讲 4.1–4.3 的精读对象。
- 错误消息拼接：`GenMessage`，所有从机过程的报错都走它，输出形如 `###ERROR###: - <Func> - <General> - <User>`。

## 4. 核心概念与源码讲解

### 4.1 等待总线条件：I2cSlaveWaitStart / WaitRepeatedStart / WaitStop

#### 4.1.1 概念说明

I2C 用 SDA 在 SCL 为高时的跳变来界定帧：SDA 由高到低（SCL 高）= **START**；SDA 由低到高（SCL 高）= **STOP**；不发 STOP 直接再来一个 START = **Repeated START**（用于混合读写，不释放总线）。

从机对这三件事都是**被动观察者**：它不驱动这些跳变，只等主机产生，并在等的过程中用 `LevelCheck` 反复确认「SCL 此刻确实应该是这个电平」。这就是 `WaitStart` 系列过程的职责——**给从机进程一个同步点**，让它知道「主机已经发车了，可以准备收/发下一字节」。

需要注意一点：`WaitRepeatedStart` 与 `WaitStop` 多了一个 `ClkStretch` 参数，而 `WaitStart` 没有。原因是 START 发生在总线空闲（SCL/SDA 都高）之后，从机此时没有任何理由拉伸时钟；而 Repeated START 与 STOP 之前总线处于「SCL 低」的字节间隙，从机可以借机拉低 SCL 拖延一下——这正是时钟拉伸的第二个入口（第一个、也是主要入口在字节级原语里，见 4.4）。

#### 4.1.2 核心流程

`I2cSlaveWaitStart` 的流程（等主机打出 START）：

1. 前置检查：SCL 必须已是高、SDA 必须已是高（否则报错）。
2. `LevelWait('0', SDA)`：等 SDA 被主机拉低（START 的下降沿）。
3. `LevelCheck('1', SCL)`：断言此跳变期间 SCL 仍为高（START 的定义）。
4. `LevelWait('0', SCL)`：等 SCL 被主机拉低（主机进入第一位的数据窗口）。
5. `LevelCheck('0', SDA)`：断言 SCL 下降时 SDA 已稳定在低。
6. `wait for ClkQuartPeriod`：走到 SCL 低电平中点，为后续 `ExpectAddr` 的位级原语对齐相位。

`I2cSlaveWaitRepeatedStart` 与 `I2cSlaveWaitStop` 形态相似，但都多了一段「若当前 SCL=0，先（可选）拉伸、再等 SCL 升高」的前奏，因为它们进入时总线多半处在 SCL 低。

#### 4.1.3 源码精读

`I2cSlaveWaitStart` 的实现，注意它完全由 `LevelCheck` + `LevelWait` 拼成，没有任何 SCL/SDA 驱动：

[hdl/psi_tb_i2c_pkg.vhd:573-592](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L573-L592) — 先查 SCL/SDA 都为高，再依次等 SDA 下降、确认 SCL 高、等 SCL 下降、确认 SDA 低，最后等 ¼ 时钟周期对齐到 SCL 低中点。

`I2cSlaveWaitRepeatedStart` 的实现，注意开头对 SCL=0 分支的处理与 `ClkStretch`：

[hdl/psi_tb_i2c_pkg.vhd:594-625](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L594-L625) — 若进入时 SCL 已被拉低，则在 `ClkStretch > 0 ns` 时先把 SCL 钳住一段时间再松手（`Scl <= '0'; wait for ClkStretch; Scl <= 'Z';`），随后等 SCL 升高、确认 SDA 在 SCL 升高前已为高，再走与 START 相同的「等 SDA 下降、确认 SCL 高、等 SCL 下降、确认 SDA 低」序列。

> 关于这里的 `to_01X(Scl)`：它来自 `psi_common_logic_pkg`，把 `'H'` 归一化成 `'1'`、`'L'` 归一化成 `'0'`。从机用它判断 SCL 现状，是为了兼容上拉产生的 `'H'` 电平——直接写 `Scl = '1'` 会在 `Scl = 'H'` 时误判。

`I2cSlaveWaitStop` 的实现，结构与 Repeated Start 对称（等 SDA 上升而非下降）：

[hdl/psi_tb_i2c_pkg.vhd:627-656](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L627-L656) — 同样有 SCL=0 时的可选拉伸前奏，随后 `LevelWait('1', SDA)` 等 SDA 被主机释放拉高（STOP 的上升沿），`LevelCheck('1', SCL)` 确认 STOP 期间 SCL 为高。

#### 4.1.4 代码实践

**实践目标**：直观感受「从机过程是被动同步点」——少了它，从机进程会与主机进程失配。

**操作步骤（源码阅读型）**：

1. 打开 [testbench/psi_tb_i2c_pkg_tb.vhd:179-181](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L179-L181)，看 `p_slave` 第一段用例：`I2cSlaveWaitStart` → `I2cSlaveExpectAddr` → `I2cSlaveWaitStop`。
2. 对照 [testbench/psi_tb_i2c_pkg_tb.vhd:48-50](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L48-L50) 的 `p_master`：`I2cMasterSendStart` → `I2cMasterSendAddr` → `I2cMasterSendStop`。
3. 思考：如果删掉从机的 `I2cSlaveWaitStart`，让从机直接调 `I2cSlaveExpectAddr`，会在哪个 `LevelWait` 上与主机错位。

**需要观察的现象**：两个进程的调用是**一一对位**的——主机 `SendStart` 的每一个 `LevelCheck`/时序步，都对应从机 `WaitStart` 里一个等电平的 `LevelWait`。这种「你打我等」的配对是 I2C 对拍的本质。

**预期结果**：能用自己的话说出「`WaitStart` 不驱动任何信号，它只是把从机进程卡在正确的相位上，等主机把 START 打出来」。若在本机跑一遍 TB，这段不会有 `###ERROR###`。

#### 4.1.5 小练习与答案

**练习 1**：`I2cSlaveWaitStart` 里既没有 `Scl <= ...` 也没有 `Sda <= ...`，它凭什么能「等」到 START？

**答案**：靠 `LevelWait`/`LevelCheck` 对信号的 `wait until`。`LevelWait` 内部用 `wait until Sig = Expected for Timeout` 挂起进程，直到主机把 SDA/SCL 驱动到目标电平；从机自己不驱动，只观察。

**练习 2**：为什么 `I2cSlaveWaitStart` 没有 `ClkStretch` 参数，而 `WaitRepeatedStart` 和 `WaitStop` 有？

**答案**：START 发生在总线空闲（SCL/SDA 均高）之后，从机没有动机拉伸；而 Repeated START 与 STOP 都处在字节间隙的 SCL 低电平窗口，从机可以借机把 SCL 钳低来拖延主机，所以这两个过程才提供 `ClkStretch` 入口。

---

### 4.2 地址期望：I2cSlaveExpectAddr

#### 4.2.1 概念说明

START 之后，主机发出的第一个字节是「地址 + R/W 位」。从机的任务是：**收下这 8 位（或 10 位地址下的前后两个字节），与自己的期望地址比对，并回一个 ACK 或 NACK**。

注意「期望」这个用词：`I2cSlaveExpectAddr` 与主机侧的 `I2cMasterSendAddr` 是镜像关系——主机**发**它想要的地址，从机**期望**收到一个特定地址。如果主机发的地址与从机 `Address` 参数不一致，从机内部的 `CheckBitExclClock` 会在对应位上打印 `###ERROR###`（这是自检失败，不是协议层面的 NACK）。也就是说：

- `AckOutput` 参数控制从机**驱动**的应答电平（`'0'`=ACK，`'1'`=NACK），是协议行为；
- 地址比对是否相符，由 BFM 自动检查，不符则报 `###ERROR###`，是仿真自检。

这两件事是独立的。

#### 4.2.2 核心流程

7 位寻址（`AddrBits = 7`）：

1. `ExpectByteExclClock(AddrSlv(6 downto 0) & Rw)`：按位收 8 位（7 位地址 + 1 位 R/W），逐位与期望比对。
2. `SendBitExclClock(AckOutput)`：从机驱动第 9 位的应答电平。
3. `I2cBusFree(Scl, Sda)`：把 SCL/SDA 都置 `'Z'`，松手，为下一字节腾出总线。

10 位寻址（`AddrBits = 10`）：把上面三步**做两遍**——第一字节发保留前缀 `11110` + 地址高 2 位 + R/W，第二字节发地址低 8 位；每字节后都回 ACK 并 `I2cBusFree`。

`Rw_c` 由 `IsRead` 决定：`choose(IsRead, '1', '0')`，读为 1、写为 0，与 I2C 协议一致。

#### 4.2.3 源码精读

`I2cSlaveExpectAddr` 的实现，看 7b/10b 两个分支如何复用 `ExpectByteExclClock` + `SendBitExclClock` + `I2cBusFree`：

[hdl/psi_tb_i2c_pkg.vhd:658-687](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L658-L687) — 7b 分支一次「收字节→回应答→松手」；10b 分支两次，第二次只发地址低 8 位（无 R/W）。`Timeout` 与 `ClkStretch` 透传给每一次位级原语调用，意味着拉伸与超时保护是**逐位**生效的。

辅助过程 `ExpectByteExclClock` 只是把 8 次 `CheckBitExclClock` 串起来：

[hdl/psi_tb_i2c_pkg.vhd:374-385](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L374-L385) — 从高位到低位循环调用 `CheckBitExclClock`，每一位都带 `Timeout` 与 `ClkStretch`。

`I2cBusFree` 的实现极简，但语义重要——它代表「本进程放弃总线驱动」：

[hdl/psi_tb_i2c_pkg.vhd:406-411](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L406-L411) — 把 SCL/SDA 都置 `'Z'`，让上拉的 `'H'` 重新生效，总线回到空闲电平。

#### 4.2.4 代码实践

**实践目标**：理解 10 位寻址下从机为什么要有「两段收+两段应答」。

**操作步骤（源码阅读型）**：

1. 读 [testbench/psi_tb_i2c_pkg_tb.vhd:194-196](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L194-L196)：从机用 `I2cSlaveExpectAddr(16#113#, false, ..., 10)` 期望一个 10 位地址。
2. 对照主机侧 [testbench/psi_tb_i2c_pkg_tb.vhd:69-71](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L69-L71)：`I2cMasterSendAddr(16#113#, false, ..., 10)`。
3. 在源码 [hdl/psi_tb_i2c_pkg.vhd:515-521](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L515-L521)（主机 10b 分支）与 [hdl/psi_tb_i2c_pkg.vhd:677-683](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L677-L683)（从机 10b 分支）之间逐字节比对：两边都先发/收 `"11110" & addr[9:8] & Rw`，再发/收 `addr[7:0]`。

**需要观察的现象**：10 位寻址时，主机和从机**各自调用两次**字节级过程，且第一字节的保留前缀 `11110` 与地址高 2 位的拼接顺序在两边完全一致。

**预期结果**：能解释「为什么 10 位地址需要两个字节」——I2C 协议用 `11110xx0` 作为保留前缀来声明这是一个 10 位地址，高 2 位跟着前缀走，低 8 位单独一字节。两边 BFM 都遵循同一打包方式，所以能对上。

#### 4.2.5 小练习与答案

**练习 1**：从机的 `AckOutput` 参数与「地址比对是否通过」有什么关系？

**答案**：没有关系。`AckOutput` 只决定从机在第 9 位**驱动**的电平（`'0'`=ACK，`'1'`=NACK），是协议层应答。地址是否与 `Address` 参数一致由 BFM 用 `CheckBitExclClock` 自动逐位比对，不符就报 `###ERROR###`。两者互不影响。

**练习 2**：`I2cSlaveExpectAddr` 在每收完一个字节后都调一次 `I2cBusFree`，为什么？

**答案**：收字节期间从机可能为了应答而驱动了 SDA（乃至位级原语里短暂钳过 SCL），`I2cBusFree` 把两根线都置 `'Z'`，让本进程彻底松手，避免在下一字节或 STOP 阶段与主机抢总线。

---

### 4.3 字节期望与发送：I2cSlaveExpectByte / I2cSlaveSendByte

#### 4.3.1 概念说明

地址握手之后进入数据阶段，方向由地址字节的 R/W 位决定：

- **写事务**（主机 → 从机）：从机**收**数据，用 `I2cSlaveExpectByte`。从机逐位读主机驱动的 SDA，收完 8 位后**驱动**第 9 位的应答（`AckOutput`）。
- **读事务**（从机 → 主机）：从机**发**数据，用 `I2cSlaveSendByte`。从机逐位驱动 SDA，发完 8 位后**松手**并**检查**主机在第 9 位给的应答（`ExpectedAck`）。

这里有一个对称的反转，务必记牢：

| | 数据 8 位由谁驱动 | 第 9 位应答由谁驱动 | 第 9 位是否校验 |
|---|---|---|---|
| `I2cSlaveExpectByte`（写） | 主机 | **从机**（`AckOutput`） | 从机主动驱动，无需校验 |
| `I2cSlaveSendByte`（读） | **从机** | 主机 | 从机用 `ExpectedAck` 校验主机应答 |

换句话说：**谁收数据，谁给应答**；应答永远由接收方驱动。所以从机收字节时它给 ACK/NACK，从机发字节时它检查主机给的 ACK/NACK。

参数命名也体现了这个反转：`ExpectByte` 用 `AckOutput`（我要**输出**的应答），`SendByte` 用 `ExpectedAck`（我**期望收到**的应答，`'0'`/`'1'` 之外的值表示「不检查」）。这与主机侧 `SendByte`/`ExpectByte` 的参数命名完全对称。

#### 4.3.2 核心流程

`I2cSlaveExpectByte`（写，收一字节）：

1. 把 `ExpData`（`-128..255`）转成 8 位 `std_logic_vector`（负数用 `to_signed`，正数用 `to_unsigned`）。
2. `ExpectByteExclClock`：循环 8 次 `CheckBitExclClock`，逐位读主机发的数据并比对。
3. `SendBitExclClock(AckOutput)`：从机驱动应答位。
4. `I2cBusFree`。

`I2cSlaveSendByte`（读，发一字节）：

1. 把 `Data` 转成 8 位 `std_logic_vector`。
2. 循环 8 次 `SendBitExclClock`，逐位驱动 SDA。
3. `I2cBusFree`：**先松手**，让主机能在第 9 位驱动 SDA。
4. `CheckBitExclClock(ExpectedAck)`：读回主机应答并（若 `ExpectedAck` 是 `'0'`/`'1'`）比对。

注意 `SendByte` 的顺序：先 `I2cBusFree` 再 `CheckBitExclClock`——因为第 9 位 SDA 由主机驱动，从机必须先松手，否则会与主机抢线。

#### 4.3.3 源码精读

`I2cSlaveExpectByte` 的实现：

[hdl/psi_tb_i2c_pkg.vhd:689-707](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L689-L707) — 数据转换后调 `ExpectByteExclClock` 收 8 位，再 `SendBitExclClock(AckOutput)` 给应答，最后 `I2cBusFree`。`Timeout`/`ClkStretch` 透传。

`I2cSlaveSendByte` 的实现，注意「先松手再查应答」的顺序：

[hdl/psi_tb_i2c_pkg.vhd:710-736](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L710-L736) — 循环 `SendBitExclClock` 发 8 位数据，随后**先** `I2cBusFree`（松手把 SDA 交给主机），**再** `CheckBitExclClock(ExpectedAck)` 读回并校验主机应答。

对照主机侧 `I2cMasterExpectByte`（读时主机收字节）：

[hdl/psi_tb_i2c_pkg.vhd:547-568](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L547-L568) — 主机先 `Sda <= 'Z'`（松手让从机驱动数据），循环 `CheckBitInclClock` 读 8 位，最后 `SendBitInclClock(AckOutput)` 给应答。把它与从机 `SendByte` 并排看，就能看到读事务里两边的「驱动/等待」完全互补。

#### 4.3.4 代码实践

**实践目标**：用「驱动方向」这一条线索把四个字节级过程（主/从 × 收/发）理清。

**操作步骤（表格填写型）**：

1. 准备一张 4 行表格，列分别为：过程名、数据 8 位谁驱动 SDA、应答位谁驱动 SDA、用哪个位级原语。
2. 依次填入 `I2cMasterSendByte`、`I2cMasterExpectByte`、`I2cSlaveExpectByte`、`I2cSlaveSendByte`。
3. 填写时参照源码：主机侧用 `SendBitInclClock`/`CheckBitInclClock`（[hdl/psi_tb_i2c_pkg.vhd:238-287](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L238-L287)），从机侧用 `SendBitExclClock`/`CheckBitExclClock`（[hdl/psi_tb_i2c_pkg.vhd:289-357](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L289-L357)）。

**需要观察的现象**：每一对对位的过程（如主机 `ExpectByte` ↔ 从机 `SendByte`），数据驱动方与应答驱动方恰好互换；且每个字节 9 个 SCL 脉冲里，前 8 个由数据发送方驱动 SDA、第 9 个由接收方驱动 SDA。

**预期结果**：得到一张自洽的表，例如「`I2cSlaveSendByte`：数据由从机驱动、应答由主机驱动、用 `SendBitExclClock`（数据）+ `CheckBitExclClock`（应答）」。

#### 4.3.5 小练习与答案

**练习 1**：`I2cSlaveSendByte` 里为什么是「先 `I2cBusFree` 再 `CheckBitExclClock`」，能不能反过来？

**答案**：不能。第 9 位的应答由主机驱动 SDA，从机必须先把 SDA 置 `'Z'` 松手（`I2cBusFree`），主机才能把 SDA 拉到目标电平；若先 `CheckBitExclClock`，从机尚未松手，SDA 还挂着从机的驱动，读回的就不是主机的应答，会误报。

**练习 2**：`I2cSlaveExpectByte` 的 `AckOutput` 与 `I2cSlaveSendByte` 的 `ExpectedAck`，哪个是「主动驱动」、哪个是「被动校验」？

**答案**：`AckOutput` 是主动驱动——从机收完字节后用它把第 9 位 SDA 拉到 ACK 或 NACK；`ExpectedAck` 是被动校验——从机发完字节后读回主机驱动的应答位，与期望值比对（`'0'`/`'1'` 之外不校验）。

---

### 4.4 Timeout 与 ClkStretch：防挂死与时钟拉伸

本节是从机侧最值得深读的部分：`Timeout` 与 `ClkStretch` 这两个参数的真正实现都在私有位级原语 `SendBitExclClock` / `CheckBitExclClock` 里，而它们被 `ExpectAddr` / `ExpectByte` / `SendByte` 逐位透传调用。

#### 4.4.1 概念说明

**Timeout（防挂死）**：从机过程全是「等主机的 SCL 边沿」。如果 DUT（这里是被测的主机实现）有 bug、永远不发某个边沿，一个朴素的 `wait until Scl = '1'` 会让从机进程**永久挂起**，整个仿真卡死、CI 永不结束。psi_tb 的做法是给每一个「等电平」都套上 `for Timeout`，并在超时后打印 `###ERROR###` 但**继续往下走**（`severity error` 不中断）。于是从机过程最坏情况是打印一条诊断信息、然后继续推进，不会把仿真挂死。默认 `Timeout = 1 ms`，对 400 kHz 的 I2C（一位 2.5 µs）来说非常宽裕。

**ClkStretch（时钟拉伸）**：真实 I2C 从机在来不及处理时，会在主机释放 SCL 后**继续把 SCL 钳在低电平**，主机检测到 SCL 没升高就会等待，从而给从机争取时间。psi_tb 在仿真里这样建模：

- 从机侧：在每一位开始前（SCL 还在低时），若 `ClkStretch > 0 ns`，主动 `Scl <= '0'; wait for ClkStretch; Scl <= 'Z';`——即把 SCL 钳低一段时间再松手。
- 主机侧：主机的位级原语在 `Scl <= 'Z'`（想让 SCL 升高）之后，并不立即继续，而是 `LevelWait('1', Scl, 1 ms, "SCL held low by other device")`——**等 SCL 真正变高**。从机钳低期间，主机就卡在这一句 `LevelWait` 上，自然实现了「主机被从机拖住」。

两者合起来，就构成了一次完整的时钟拉伸握手。注意主机那个 `LevelWait` 的超时是**硬编码 1 ms**（不是从机的 `Timeout` 参数），所以从机的 `ClkStretch` 必须明显小于 1 ms，否则主机反而会报 `SCL held low by other device`。

#### 4.4.2 核心流程

`SendBitExclClock`（从机发一位）的流程，是 `ClkStretch` 的主战场：

1. `LevelCheck('0', Scl)`：进入时 SCL 必须为低。
2. **时钟拉伸**：若 `ClkStretch > 0 ns`，`Scl <= '0'; wait for ClkStretch;`（钳低），置 `Stretched_v := true`。
3. 驱动本位数据到 SDA（`'0'` 拉低、`'1'` 松手 `'Z'`）。
4. 若拉伸过，`wait for ClkQuartPeriod; Scl <= 'Z';`（松手让 SCL 能升高）。
5. `LevelWait('1', Scl, Timeout)`：等主机把 SCL 推高（上升沿）——注意这一步同时受 `Timeout` 保护。
6. `LevelWait('0', Scl, Timeout)`：等主机把 SCL 拉低（下降沿）。
7. `LevelCheck(Data, Sda)` + `CheckLastActivity(Sda, ...)`：回读 SDA，确认数据正确且 SCL 高电平期间 SDA 稳定（复用 u4-l1 的活动检查）。
8. `wait for ClkQuartPeriod`：走到 SCL 低中点。

`CheckBitExclClock`（从机读一位）结构相仿，区别在于不驱动数据 SDA，只读回比对；拉伸逻辑在第 2 步相同位置。

可以这样理解一次「带拉伸的位」的时间消耗（记 `T = ClkPeriod = 1/FreqClk`，`S = ClkStretch`）：

\[
t_{\text{bit, stretched}} \;\approx\; \tfrac{1}{4}T \;(\text{建}) \;+\; S \;(\text{拉伸}) \;+\; \tfrac{1}{2}T \;(\text{SCL 高}) \;+\; \tfrac{1}{4}T \;(\text{SCL 低中点}) 
\]

而无拉伸时 \(S = 0\)，与主机 `SendBitInclClock` 的 `¼T + ½T + ¼T` 节拍对齐。

#### 4.4.3 源码精读

`SendBitExclClock` 的实现，重点看开头的拉伸段与随后的两个 `LevelWait`：

[hdl/psi_tb_i2c_pkg.vhd:289-329](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L289-L329) — 第 302-306 行是拉伸核心：`if ClockStretch > 0 ns then Scl <= '0'; wait for ClockStretch; Stretched_v := true; end if;`；第 314-317 行在驱动数据后补一个 ¼ 周期再 `Scl <= 'Z'` 松手；第 320、323 行的两个 `LevelWait` 分别等 SCL 升高与降低，都带 `Timeout`，这是 `Timeout` 防挂死的落点。

`CheckBitExclClock` 的实现，拉伸段更紧凑：

[hdl/psi_tb_i2c_pkg.vhd:331-357](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L331-L357) — 第 343-347 行 `if ClockStretch > 0 ns then Scl <= '0'; wait for ClockStretch; Scl <= 'Z'; end if;`，随后 `LevelWait('1', Scl, Timeout)` 与 `LevelWait('0', Scl, Timeout)` 等主机的两个边沿。

`LevelWait` 自身的实现，看清「超时也返回」：

[hdl/psi_tb_i2c_pkg.vhd:199-218](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L199-L218) — `wait until Sig = Expected for Timeout`，超时后用 `Correct_v` 记录是否真的达标，不达标只 `assert ... severity error`（打印 `###ERROR###`）然后正常返回，绝不挂死。

主机侧「被拉伸」的检测点，注意那句诊断信息：

[hdl/psi_tb_i2c_pkg.vhd:255-258](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L255-L258) — 主机 `SendBitInclClock` 在 `Scl <= 'Z'` 后用 `LevelWait('1', Scl, 1 ms, "SCL held low by other device")` 等 SCL 升高；从机若在钳低 SCL，主机就卡在这里，等从机松手。`CheckBitInclClock` 在 [hdl/psi_tb_i2c_pkg.vhd:278-281](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L278-L281) 有完全相同的一句。

官方 TB 里启用时钟拉伸的真实用例：

[testbench/psi_tb_i2c_pkg_tb.vhd:254-265](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L254-L265) — `Clock Stretching` 段，从机在读取事务里给 `ExpectAddr` 与 `SendByte` 都传 `ClkStretch => 10 us`（[第 257-258 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L257-L258)），在写事务里给 `ExpectAddr` 传 5 µs、`ExpectByte` 传 7 µs（[第 263-264 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L263-L264)）；对应的主机侧 [testbench/psi_tb_i2c_pkg_tb.vhd:152-164](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L152-L164) 用的是普通（无拉伸感知）调用，却仍能完成握手——这正说明主机 `LevelWait('1', Scl, 1 ms)` 自动吸收了从机的拉伸。

#### 4.4.4 代码实践

**实践目标**：亲手实现「读取事务中从机用 `ClkStretch` 拉低 SCL 一段时间，主机侧仍正确完成握手」，并观察波形上的拉伸缺口。

**操作步骤（在官方 TB 上扩展）**：

1. 复制 [testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd) 到本机（或直接在 `p_slave` 末尾、`wait;` 之前追加用例）。
2. 在 `p_master` 末尾（[第 164 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L164) `wait for 10 us;` 之后、`wait;` 之前）追加一次读取：
   ```vhdl
   -- 示例代码：主机发起一次读取（普通调用，无拉伸参数）
   print("My Stretch Read");
   I2cMasterSendStart(scl, sda, "M: start");
   I2cMasterSendAddr(16#13#, true, scl, sda, "M: address", 7);
   I2cMasterExpectByte(16#7E#, scl, sda, "M: data-read", '1');  -- 末字节 NACK
   I2cMasterSendStop(scl, sda, "M: stop");
   wait for 10 us;
   ```
3. 在 `p_slave` 末尾（[第 265 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L265) `I2cSlaveWaitStop` 之后、`wait;` 之前）追加对拍的从机序列，**重点是在 `SendByte` 上加大幅拉伸**：
   ```vhdl
   -- 示例代码：从机对拍，SendByte 每位拉伸 20 us
   I2cSlaveWaitStart(scl, sda, "S: wait start");
   I2cSlaveExpectAddr(16#13#, true, scl, sda, "S: check address", 7);
   I2cSlaveSendByte(16#7E#, scl, sda, "S: data-read", '1', 1 ms, 20 us);
   I2cSlaveWaitStop(scl, sda, "S: wait stop");
   ```
4. 按 u1-l3 的方法跑 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）。

**需要观察的现象**：

- 仿真正常结束，Transcript 出现 `SIMULATIONS COMPLETED SUCCESSFULLY`，且**没有** `###ERROR###`，也没有 `SCL held low by other device`——说明主机容忍了 20 µs 的拉伸。
- 在波形上，`SendByte` 的 8 个数据位里，每一位的 SCL 低电平都会多出一段约 20 µs 的「平台」（相对正常 400 kHz 下一位仅 2.5 µs），这就是时钟拉伸缺口；对应的，主机进程卡在 `LevelWait('1', Scl, 1 ms)` 等这一缺口结束。
- 把第 3 步的 `20 us` 改成 `2 ms`（超过主机的 1 ms 上限）再跑：这次主机会在某一位打印 `###ERROR###: - I2cMasterExpectByte - SCL held low by other device - M: data-read`，CI 因此判定失败——这验证了主机对拉伸有时长上限。

**预期结果**：`ClkStretch = 20 us` 时握手成功、无报错；`ClkStretch = 2 ms` 时主机报 `SCL held low by other device`。若你无法在本机运行仿真，相关结论标注为「待本地验证」，但「主机用 `LevelWait('1', Scl, 1 ms)` 吸收从机拉伸、上限为 1 ms」这一机制可直接由 [hdl/psi_tb_i2c_pkg.vhd:255-258](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L255-L258) 的源码确认。

#### 4.4.5 小练习与答案

**练习 1**：把从机某个 `ExpectByte` 的 `Timeout` 设成 `100 ns`（远小于一位的时间），会发生什么？

**答案**：从机在 `CheckBitExclClock` 里 `LevelWait('1', Scl, 100 ns)` 几乎必定超时（400 kHz 下 SCL 低半周期就有 1.25 µs），于是打印 `###ERROR###: - I2cSlaveExpectByte - SCL did not go high - ...`，但因为 `severity error` 不中断、`LevelWait` 超时后照常返回，从机进程会带着一连串报错继续跑完。这正说明 `Timeout` 是「防挂死 + 留诊断」而非「正确性门槛」。

**练习 2**：主机侧检测时钟拉伸的 `LevelWait('1', Scl, 1 ms)` 与从机侧产生拉伸的 `Scl <= '0'; wait for ClkStretch;` 是如何「对上」的？为什么主机不会因为从机钳低而立即报错？

**答案**：主机在 `Scl <= 'Z'`（想升高 SCL）之后，并不假定 SCL 立刻就高，而是 `LevelWait('1', Scl, 1 ms)` **主动等** SCL 变高。从机把 SCL 钳在 `'0'` 期间，上拉无法把它推高，主机的 `LevelWait` 就一直挂起；直到从机 `Scl <= 'Z'` 松手，上拉把 SCL 推高，主机的 `LevelWait` 才解除。只要拉伸时长 `< 1 ms`，主机就不会触发超时报错。

**练习 3**：`ClkStretch` 为什么在字节级过程里「逐位」生效，而不是整字节一次性拉伸？

**答案**：因为 `ExpectAddr`/`ExpectByte`/`SendByte` 把 `ClkStretch` 透传给**每一位**的 `SendBitExclClock`/`CheckBitExclClock`，而拉伸代码写在位级原语里。所以一次 `I2cSlaveSendByte(..., ClkStretch => 10 us)` 实际是 8 个数据位**每位**都拉伸 10 µs（应答位那拍的 `CheckBitExclClock` 也会拉伸）。这让每位之间都有均匀的「思考时间」，更接近真实从机「逐字节乃至逐位都可拉伸」的行为。

## 5. 综合实践

把本讲四块知识串起来，完成一个**带时钟拉伸的混合读写事务**的从机对拍。

**任务**：在官方 TB 的 `p_slave` 里实现如下从机行为，并与 `p_master` 对拍成功（无 `###ERROR###`）：

1. 等待 START，期望 7 位地址 `0x13`、**写**方向，回 ACK。
2. 收 1 字节期望 `0x55`，回 ACK（普通速度）。
3. **Repeated START**（用 `I2cSlaveWaitRepeatedStart`），期望 7 位地址 `0x13`、**读**方向，回 ACK。
4. **发** 1 字节 `0x99`，且在读回路径上对每位施加 `ClkStretch => 15 us` 的时钟拉伸；末字节期望主机回 NACK（`ExpectedAck => '1'`）。
5. 等待 STOP。

**自检要点**：

- 步骤 3 必须用 `I2cSlaveWaitRepeatedStart` 而不是 `WaitStop`+`WaitStart`——否则总线会被释放，主机侧的 `I2cMasterSendRepeatedStart` 会因为 SDA/SCL 时序对不上而报错。
- 步骤 4 的 `SendByte` 要同时给出 `ExpectedAck`（第 5 参数）与 `ClkStretch`（第 7 参数），中间的 `Timeout`（第 6 参数）用默认 `1 ms` 即可，调用形如 `I2cSlaveSendByte(16#99#, scl, sda, "S: read", '1', 1 ms, 15 us);`。
- 主机侧对应写一段：`SendStart → SendAddr(0x13, 写) → SendByte(0x55) → SendRepeatedStart → SendAddr(0x13, 读) → ExpectByte(0x99, NACK) → SendStop`。

**预期结果**：仿真跑通，Transcript 末尾出现 `SIMULATIONS COMPLETED SUCCESSFULLY`，无任何 `###ERROR###`；波形上能看到 Repeated START 前后总线未释放（无 STOP 间隙），以及读字节阶段每位 SCL 低电平被拉伸出的 15 µs 平台。这一任务把「等待总线条件（4.1）→ 地址期望（4.2）→ 字节收发与方向反转（4.3）→ 时钟拉伸与超时保护（4.4）」全部用到了。

## 6. 本讲小结

- 从机侧六个公开过程都是**被动**的：`WaitStart/WaitRepeatedStart/WaitStop` 只观察 SDA/SCL 跳变来同步帧，`ExpectAddr/ExpectByte/SendByte` 只在「等主机的 SCL 边沿」之间驱动或校验 SDA。
- 命名体现分工：从机全程走 `ExclClock`（不含时钟）位级原语，**等**主机打出的 SCL；主机侧对应 `InclClock`（含时钟），自己产生 SCL，并在 `Scl <= 'Z'` 后用 `LevelWait('1', Scl, 1 ms, "SCL held low by other device")` 检测并等待从机的拉伸。
- 数据方向决定应答方向：**谁收数据谁给应答**。`ExpectByte`（写）由从机驱动 `AckOutput`，`SendByte`（读）由从机先 `I2cBusFree` 松手再 `CheckBitExclClock(ExpectedAck)` 校验主机应答。
- `Timeout` 是防挂死机制：所有「等电平」都带 `for Timeout`，超时只打印 `###ERROR###` 并继续（`severity error` 不中断），最坏也不卡死仿真；默认 `1 ms`。
- `ClkStretch` 是时钟拉伸建模：在位级原语里 `Scl <= '0'; wait for ClkStretch; Scl <= 'Z';`，逐位生效；必须小于主机的 1 ms 上限，否则主机反报 `SCL held low by other device`。
- 所有从机报错都经 `GenMessage` 拼成 `###ERROR###: - <Func> - <General> - <User>`，与 CI 的 `run_check_errors "###ERROR###"` 联动，一次失败即变 CI 失败。

## 7. 下一步学习建议

- **进入 u7-l4**：精读 [testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd) 的 `p_master` 与 `p_slave` 两个并发进程如何**成段对拍**、用 `print(">> ...")` 分节管理长用例，本讲的所有过程都会在那里以真实顺序出现。
- **回顾 u4-l1**：本讲位级原语里反复出现的 `CheckLastActivity(Sda, ClkHalfPeriod, -1, ...)` 来自 `psi_tb_activity_pkg`，想看清「SCL 高电平期间 SDA 必须稳定」是如何被检查的，可重读 u4-l1 的快照式活动检查。
- **动手扩展**：仿照 u8-l2 的「按既有约定新增过程」思路，尝试写一个 `I2cSlaveExpectBytes`（批量收多字节）的薄包装，复用 `I2cSlaveExpectByte` 并统一 `Timeout`/`ClkStretch`/`Prefix`，体会 compare→activity→bfm 的复用链。
- **跨协议对照**：把本讲的 `ExclClock`/`InclClock` 分工与 u5 的 AXI BFM 对照——AXI 用 `valid`/`ready` 握手，I2C 用开漏电平 + 时钟拉伸，两者都在 testbench 里建模了「对方未就绪就等待」的语义，对比阅读能加深对 BFM 设计模式的理解。
