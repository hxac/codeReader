# I2C 主机事务

## 1. 本讲目标

本讲承接 [u7-l1](u7-l1-i2c-overview-and-setup.md)（已讲清 I2C 开漏总线模型、`I2cPullup`/`I2cBusFree`/`I2cSetFrequency` 等初始化原语），进入 `psi_tb_i2c_pkg` 的**主机侧事务**。

学完后你应当能够：

- 说清「主机拥有时钟」这件事在代码里如何体现，并能区分 `...InclClock`（主机自己产生 SCL 脉冲）与从机侧的 `...ExclClock`（等别人给的时钟）。
- 按正确顺序调用 `I2cMasterSendStart` / `I2cMasterSendRepeatedStart` / `I2cMasterSendAddr` / `I2cMasterSendByte` / `I2cMasterExpectByte` / `I2cMasterSendStop`，拼出一次完整的 I2C 读、写或「写后读」混合事务。
- 理解 `ExpectedAck`（校验从机的应答）与 `AckOutput`（主机主动驱动的应答）这两个参数的方向差异与取值含义，知道 `'0'`/`'1'`/「其它值」分别代表什么。
- 能读懂主机过程失败时打印的 `###ERROR###` 消息格式，并把它与 CI 的通过/失败判定联系起来。

---

## 2. 前置知识

本讲默认你已经掌握 [u7-l1](u7-l1-i2c-overview-and-setup.md) 的内容，这里只做最关键的回顾：

- **I2C 是两线开漏总线**：SCL（时钟）、SDA（数据）。器件想输出 `'0'` 就把线拉低（`<= '0'`）；想输出 `'1'` 就「松手」（`<= 'Z'`），由上拉电阻把线拉到 `'H'`。所以「线上的高电平」在仿真里通常以 `'H'` 出现，判高时必须同时认 `'1'` 和 `'H'`。
- **上拉与初始化**：`I2cPullup` 常驻驱动 `'H'`；每个进程开工前调 `I2cBusFree` 释放自己对总线的驱动（`<= 'Z'`）；`I2cSetFrequency` 把位频率写入包内的 `shared variable FreqClk_v`（默认 100 kHz），位时序由几个 `impure function`（`ClkPeriod`/`ClkHalfPeriod`/`ClkQuartPeriod`）实时换算。
- **数据只在 SCL 低电平期间变化**；SDA 在 **SCL 为高**时的跳变被定义为特殊事件：下降沿 = START/Repeated START，上升沿 = STOP。
- **应答（ACK）低有效**：`I2c_ACK='0'`、`I2c_NACK='1'`（见 [hdl/psi_tb_i2c_pkg.vhd:L28-L29](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L28-L29)）。

还需要记得 [u3-l1](u3-l1-compare-basic.md) 与 [u4-l1](u4-l1-activity-check.md) 的两个底层过程，主机事务全程在复用它们：

- `LevelCheck(Expected, Sig, ...)`：即时断言 `Sig` 等于 `Expected`，判高时认 `'H'`，失败按统一前缀打印 `###ERROR###`。
- `CheckLastActivity(Sig, IdleTime, Level, ...)`：用 `Sig'last_event` 断言信号在最近 `IdleTime` 内没有翻转；`Level=-1` 表示「只查活动、不查电平」（见 [hdl/psi_tb_activity_pkg.vhd:L129-L144](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L129-L144)）。

---

## 3. 本讲源码地图

本讲只涉及一个源文件，但要把里面的「公共底座」和「主机事务」分清楚：

| 代码区域 | 作用 | 与本讲关系 |
|---|---|---|
| 消息生成 `GenMessage` / `GenMessageNoPrefix`、记录 `MsgInfo_r` | 把 `Prefix/Func/General/User` 拼成一行错误消息 | 决定主机报错的格式 |
| 电平原语 `LevelCheck` / `LevelWait` | 判/等 SCL、SDA 电平（认 `'H'`） | 所有主机过程的底层 |
| 位传输原语 `SendBitInclClock` / `CheckBitInclClock`、字节封装 `SendByteInclClock` | **主机独有**：一边搬数据一边自己产生 SCL 脉冲 | 本讲的概念核心 |
| 主机事务 `I2cMasterSendStart` / `SendRepeatedStart` / `SendStop` | 产生 START / Repeated START / STOP 条件 | 最小模块 1 |
| 主机事务 `I2cMasterSendAddr` | 发 7b/10b 地址 + R/W，并校验 ACK | 最小模块 2 |
| 主机事务 `I2cMasterSendByte` / `I2cMasterExpectByte` | 写一字节（校验 ACK）/ 读一字节（驱动 ACK） | 最小模块 3 |

参考用真实 testbench：[testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd)，里面的 `p_master`/`p_slave` 两个并发进程成对调用主机/从机过程，是本讲实践的直接依据。

---

## 4. 核心概念与源码讲解

### 4.1 底层原语：主机如何「拥有时钟」（SendBitInclClock / CheckBitInclClock）

#### 4.1.1 概念说明

I2C 总线上**只有主机产生时钟**。这件事在 `psi_tb_i2c_pkg` 里被编码成一个命名约定：

- `...InclClock`（Inclusive Clock，含时钟）：过程**自己**驱动 SCL 产生脉冲——这是**主机**用的。
- `...ExclClock`（Exclusive Clock，不含时钟）：过程**等**外部（主机）给的 SCL 脉冲——这是**从机**用的，留到 [u7-l3](u7-l3-i2c-slave-and-clock-stretching.md) 讲。

所以本讲的全部主机事务，最终都落到两个位级原语上：`SendBitInclClock`（写一位 + 出时钟）和 `CheckBitInclClock`（读/校验一位 + 出时钟）。理解了它们，后面三个最小模块都只是「调用次数与方向」的组合。

#### 4.1.2 核心流程

`SendBitInclClock`（写一位）把一个完整比特周期切成三段，总时长恰为一个 `ClkPeriod`：

```
入口断言 SCL=0
[1] 在 SDA 上摆数据：'0'→拉低，'1'→释放('Z')      wait ClkQuartPeriod   (建立时间)
[2] 释放 SCL('Z')＝主机产生上升沿                  wait ClkHalfPeriod    (SCL 高电平段)
    ─ LevelWait('1')：等 SCL 真的变高（容忍从机时钟拉伸）
    ─ CheckLastActivity(SCL)：高电平期间无人把 SCL 拉低
    ─ LevelCheck(SDA 回读)：SDA 与自己驱动的一致（仲裁/竞争检查）
    ─ CheckLastActivity(SDA)：SCL 高期间 SDA 稳定（I2C 硬规则）
[3] 拉低 SCL('0')＝主机产生下降沿                  wait ClkQuartPeriod   (回到 SCL 低中点)
```

设位频率为 \(f\)，则 \(T_{\text{bit}} = \tfrac{1}{4}T + \tfrac{1}{2}T + \tfrac{1}{4}T = T = \frac{1}{f}\)，且 SCL 高、低各占半个周期（对称）。过程结束时指针停在「SCL 低电平中点」，正好可以无缝接下一个 `SendBitInclClock`。

`CheckBitInclClock`（读一位）骨架几乎相同，唯一差别：它**不驱动数据**，而是在 SCL 高电平段用 `LevelCheck(Data, Sda, ...)` 把「线上的实际值」与传入的期望值 `Data` 比对。也就是说：**主机读的时候，仍然是主机出时钟，且每读一位都顺带自检**——这就是为什么后面的 `I2cMasterExpectByte` 必须传入 `ExpData`，没有「盲读」。

> 关键直觉：主机过程没有「发了就不管」的模式。`SendBitInclClock` 会回读 SDA 验证自己写进去的值，`CheckBitInclClock` 会校验读到的值。这意味着主机过程**不能对着一条空总线独自跑通**——它必须有从机进程在另一端按同样的预期配合握手，否则必然打印 `###ERROR###`。这一点直接决定了本讲综合实践必须写「主从成对」两个进程。

#### 4.1.3 源码精读

位传输原语（写一位）完整实现见 [hdl/psi_tb_i2c_pkg.vhd:L238-L264](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L238-L264)，关键片段（保留行内语义）：

```vhdl
-- Assert Data：'0' 拉低，其余释放让上拉拉高
if Data = '0' then  Sda <= '0';
else                Sda <= 'Z';  end if;
wait for ClkQuartPeriod;                              -- 建立时间
Scl <= 'Z';                                           -- 主机产生 SCL 上升沿
LevelWait('1', Scl, 1 ms, Msg, "SCL held low by other device");  -- 容忍时钟拉伸
wait for ClkHalfPeriod;                               -- SCL 高电平段
CheckLastActivity(Scl, ClkHalfPeriod*0.9, -1, ...);   -- 高电平期间 SCL 无人拉低
LevelCheck(Data, Sda, Msg, "SDA readback ... ");      -- 回读 SDA，验证与驱动一致
CheckLastActivity(Sda, ClkHalfPeriod, -1, ...);       -- SCL 高期间 SDA 必须稳定
Scl <= '0';                                           -- 主机产生 SCL 下降沿
wait for ClkQuartPeriod;                              -- 回到 SCL 低中点
```

读/校验一位的 `CheckBitInclClock` 见 [hdl/psi_tb_i2c_pkg.vhd:L266-L287](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L266-L287)，其校验语句是 `LevelCheck(Data, Sda, Msg, "Received wrong data [" & BitInfo & "]")`——注意 `Data` 在这里是**期望值**。

字节封装 `SendByteInclClock` 只是循环调用 8 次 `SendBitInclClock`，高位在前（`for i in 7 downto 0 loop`），见 [hdl/psi_tb_i2c_pkg.vhd:L361-L370](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L361-L370)。

错误消息的拼装规则见 `GenMessage`：[hdl/psi_tb_i2c_pkg.vhd:L168-L175](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L168-L175)，格式恒为 `Prefix & "- " & Func & " - " & General & " - " & User`。这是本讲所有报错文本的来源。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在脑子里把一个比特周期的时序与四条检查一一对应。
2. **步骤**：打开 [hdl/psi_tb_i2c_pkg.vhd:L238-L264](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L238-L264)，画一条时间轴，标出 `ClkQuartPeriod`（建立）、`ClkHalfPeriod`（SCL 高）、`ClkQuartPeriod`（回低中点）三段，并在每段上注明对应的 `LevelCheck` / `CheckLastActivity`。
3. **现象**：你会发现 SCL 的上升沿与下降沿都由 `Scl <= 'Z'` / `Scl <= '0'` 这两行主机代码主动产生——这就是「主机拥有时钟」的代码证据。
4. **预期**：一张能解释「为何 SCL 高电平期间 SDA 必须稳定」「为何回读 SDA 等于做仲裁」的时序草图。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `SendBitInclClock` 在 SCL 高电平段要做 `LevelCheck(Data, Sda, ...)` 回读？去掉它会丢失什么能力？
  - **答案**：回读用来发现「我释放了 SDA 想发 1，但线被别的器件拉成了 0」——也就是 I2C 仲裁/总线竞争。去掉它，主机就无法察觉自己丢失了仲裁，会误以为发送成功。
- **练习 2**：`ClkHalfPeriod` 与 `ClkQuartPeriod` 分别由哪个 `impure function` 算出？它们依赖什么变量？
  - **答案**：见 [hdl/psi_tb_i2c_pkg.vhd:L222-L235](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L222-L235)，分别是 `(0.5 sec)/FreqClk_v` 与 `(0.25 sec)/FreqClk_v`，都读 `I2cSetFrequency` 写入的共享变量 `FreqClk_v`。

---

### 4.2 起始、重复起始与停止条件（I2cMasterSendStart / SendRepeatedStart / SendStop）

#### 4.2.1 概念说明

正常数据位「SCL 低时改 SDA」，而 START / STOP 是**故意在 SCL 为高时翻转 SDA**的特殊事件：

- **START**：SCL 高时，SDA 由高到低。
- **STOP**：SCL 高时，SDA 由低到高。
- **Repeated START**：在一次事务中间（不发 STOP）再发一次 START，用于「写完接着读」之类的混合事务，避免被别的主机抢占总线。

`psi_tb_i2c_pkg` 把这三件事分别封装成三个过程，**都不碰数据，只摆 SDA/SCL 的电平**。

#### 4.2.2 核心流程

`I2cMasterSendStart` 入口要求总线空闲（SCL=1 且 SDA=1），然后：

```
入口断言 SCL=1, SDA=1（认 'H'）
wait ClkQuartPeriod
Sda<='0'            ← SCL 仍为高时的下降沿 = START
断言 SCL 仍为 1
wait ClkQuartPeriod
Scl<='0'            ← 拉低 SCL，进入「第一位前的低中点」
wait ClkQuartPeriod
```

`I2cMasterSendRepeatedStart` 与 `SendStart` 的差别在于：它可能在前一字节结束后被调用，此时 **SCL 已经是 0**，所以要多一段「先把 SDA 释放回高、再把 SCL 释放回高」的回卷动作，然后才发那个 SCL 高时的 SDA 下降沿。它还会检查「SDA 没被别的器件拉低」「SCL 没被别的器件拉低」。

`I2cMasterSendStop` 反过来：把 SDA 从低拉回高（释放），且这段上升沿发生在 SCL 为高期间。它同样要先处理「入口 SCL 可能是 0」的情况——先把 SDA 拉低、再释放 SCL，最后释放 SDA 形成 STOP。结束时停在「SCL 高电平中点」，即总线回到空闲。

#### 4.2.3 源码精读

`I2cMasterSendStart` 见 [hdl/psi_tb_i2c_pkg.vhd:L418-L437](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L418-L437)，关键片段：

```vhdl
LevelCheck('1', Scl, MsgInfo, "SCL must be 1 before procedure is called");
LevelCheck('1', Sda, MsgInfo, "SDA must be 1 before procedure is called");
wait for ClkQuartPeriod;
Sda <= '0';                                            -- SCL 高时的下降沿 = START
LevelCheck('1', Scl, MsgInfo, "SCL must be 1 during SDA falling edge");
wait for ClkQuartPeriod;
Scl <= '0';                                            -- 进入第一位前的低中点
wait for ClkQuartPeriod;
```

`I2cMasterSendRepeatedStart` 见 [hdl/psi_tb_i2c_pkg.vhd:L439-L467](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L439-L467)。注意它的入口分支 `if Scl = '0' then ... end if`——这就是「从前一字节末尾续接」的处理：先 `Sda <= 'Z'`、`Scl <= 'Z'` 把总线卷回 SCL/SDA 双高，再制造 START 下降沿。

`I2cMasterSendStop` 见 [hdl/psi_tb_i2c_pkg.vhd:L469-L496](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L469-L496)，制造 STOP 的那行是 `Sda <= 'Z'`（释放＝上升沿），紧随其后的 `LevelCheck('1', Scl, ...)` 断言「SDA 上升沿期间 SCL 必须为高」。

> 报错示例：若在 SCL 不为高时调用 `I2cMasterSendStart`，会打印
> `###ERROR###: - I2cMasterSendStart - SCL must be 1 before procedure is called - <你的 Msg>`。

#### 4.2.4 代码实践（阅读 + 波形观察）

1. **目标**：看清 `SendStart` 与 `SendRepeatedStart` 的入口差异。
2. **步骤**：对照 testbench 的「Repeated Start」用例 [testbench/psi_tb_i2c_pkg_tb.vhd:L137-L146](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L137-L146)。注意它在一字节写（`I2cMasterSendByte`）之后**没有**调用 `I2cMasterSendStop`，而是直接调 `I2cMasterSendRepeatedStart`——这正是 `SendRepeatedStart` 里 `if Scl = '0'` 分支要处理的场景。
3. **现象**：在波形里应看到 SCL 在「写字节末尾的低电平」与「Repeated Start 的 SDA 下降沿」之间，先被释放回高、再被拉低，中间没有出现 STOP 的 SDA 上升沿。
4. **预期**：能指出 Repeated Start 与普通 Start 在波形上的唯一区别是「它前面没有 STOP」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `I2cMasterSendRepeatedStart` 要检查 `LevelCheck('1', Sda, ..., "SDA held low by other device")`？
  - **答案**：它刚把 `Sda <= 'Z'` 试图让 SDA 回高，若此时 SDA 仍为 0，说明有别的器件在拉低总线（竞争/异常），贸然发 START 会违反协议，因此先断言。
- **练习 2**：`I2cMasterSendStop` 结束时停在「SCL 高电平中点」，这与 `SendStart` 结束停在「SCL 低电平中点」不同，为什么这样设计？
  - **答案**：STOP 之后总线回到空闲（SCL/SDA 双高），下一条事务会重新从 `SendStart` 开始并自带时序对齐；而 START 之后要紧接着发数据位，必须停在 SCL 低中点以便直接接入 `SendBitInclClock`。

---

### 4.3 地址发送与 ACK/NACK 校验（I2cMasterSendAddr）

#### 4.3.1 概念说明

`I2cMasterSendAddr` 发送地址帧并校验从机应答。I2C 的地址帧固定一字节：

- **7 位寻址**：`[7 位地址][R/W]`，R/W=1 读、0 写。
- **10 位寻址**：先发 `11110[Addr9..8][R/W]`，从机 ACK 后再发 `Addr7..0`，共两字节；每字节后都要 ACK。

关键点是**第 9 个时钟（ACK 位）的方向**：地址/数据是主机发的，所以 ACK 必须由**从机**驱动。主机的角色是「发完 8 位后释放 SDA、再出一个时钟，把从机驱动的 ACK 读回来比对」。`I2cMasterSendAddr` 用 `ExpectedAck` 参数表达对比预期。

#### 4.3.2 核心流程

```
把 Address 转 10 位 slv，按 IsRead 算 R/W 位 Rw_c
if 7 位:
    SendByteInclClock( Addr[6:0] & Rw )     -- 8 个数据位 + 主机出时钟
    Sda <= 'Z'                               -- 释放 SDA，让从机驱动 ACK
    CheckBitInclClock( ExpectedAck, "ACK" )  -- 第 9 拍：读回 ACK 并比对
elsif 10 位:
    SendByteInclClock( "11110" & Addr[9:8] & Rw ); 释放; 校验 ACK
    SendByteInclClock( Addr[7:0] );          释放; 校验 ACK
else:
    report "Illegal addrBits (must be 7 or 10)"
```

`ExpectedAck` 的三态语义（注释见 [hdl/psi_tb_i2c_pkg.vhd:L504](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L504)）：

| `ExpectedAck` | 含义 | 如何实现 |
|---|---|---|
| `'0'` | 期望从机 ACK（拉低） | `LevelCheck('0', Sda)` 要求 SDA=0 |
| `'1'` | 期望从机 NACK（松手高） | `LevelCheck('1', Sda)` 要求 SDA=1/H |
| 其它（如 `'Z'`/`'H'`/`'-'`） | 不校验 | `LevelCheck` 入口 `if (Expected='0') or (Expected='1')` 不成立，直接跳过 |

这是因为 `LevelCheck` 只在期望值是 `'0'` 或 `'1'` 时才发断言（见 [hdl/psi_tb_i2c_pkg.vhd:L186-L197](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L186-L197)）。

#### 4.3.3 源码精读

完整过程见 [hdl/psi_tb_i2c_pkg.vhd:L498-L525](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L498-L525)，7 位寻址片段：

```vhdl
constant AddrSlv_c : std_logic_vector(9 downto 0) := std_logic_vector(to_unsigned(Address, 10));
constant Rw_c      : std_logic := choose(IsRead, '1', '0');   -- 来自 psi_common_math_pkg
...
if AddrBits = 7 then
    SendByteInclClock(AddrSlv_c(6 downto 0) & Rw_c, Scl, Sda, (Prefix, "I2cMasterSendAddr 7b", Msg));
    Sda <= 'Z';                                                -- 释放，让从机驱动 ACK
    CheckBitInclClock(ExpectedAck, Scl, Sda, "ACK", (Prefix, "I2cMasterSendAddr 7b", Msg));
```

注意 `&` 把 7 位地址与 1 位 R/W 拼成正好 8 位送给 `SendByteInclClock`。`choose` 是 `psi_common_math_pkg` 提供的三目运算函数。

> 报错示例：若主机 `ExpectedAck='0'`（要 ACK）但从机实际 NACK（SDA 为 `'H'`），`CheckBitInclClock` 里的 `LevelCheck` 失败，打印
> `###ERROR###: - I2cMasterSendAddr 7b - Received wrong data [ACK] - <你的 Msg>`。

#### 4.3.4 代码实践（修改参数观察）

1. **目标**：体会 `ExpectedAck` 三态语义。
2. **步骤**：在 testbench 的地址用例里（[testbench/psi_tb_i2c_pkg_tb.vhd:L60-L65](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L60-L65) 的「NACK read」用例），主从两侧都已配成 NACK。把**主机侧** `I2cMasterSendAddr(..., 7, '1')` 中的 `'1'` 临时改成 `'0'`（即主机期望 ACK、从机却发 NACK），重新仿真。
3. **现象**：Transcript 里应出现一行 `###ERROR###: - I2cMasterSendAddr 7b - Received wrong data [ACK] - M: 7b address`。
4. **预期**：验证「主从 ACK 期望不一致 → 主机自检失败 → 打印 `###ERROR###` → 被 CI 的 `run_check_errors` 捕获」。若把 `'1'` 改成 `'Z'`（不校验），则即使从机 NACK，主机也**不会**报错。
5. **待本地验证**：实际运行需要按 [u1-l3](u1-l3-simulation-and-ci.md) 用 `sim/run.tcl` 或 `sim/runGhdl.tcl` 跑通；本步骤未替你执行命令。

#### 4.3.5 小练习与答案

- **练习 1**：10 位寻址时，主机先发的字节 `11110XXR` 中 `XX` 是地址的哪几位？为什么要前缀 `11110`？
  - **答案**：`XX` 是 `Addr[9:8]`（最高两位）。`11110` 是 I2C 协议规定的 10 位地址保留前缀，用于让总线上只支持 7 位寻址的器件知道这不是一个普通 7 位地址、不要误响应。
- **练习 2**：把 `ExpectedAck` 设成 `'H'` 会发生什么？为什么？
  - **答案**：不校验 ACK。因为 `LevelCheck` 的入口 `if (Expected='0') or (Expected='1')` 对 `'H'` 不成立，直接跳过断言，主机照常出第 9 个时钟但不比对从机应答。

---

### 4.4 数据写与数据读（I2cMasterSendByte / I2cMasterExpectByte）

#### 4.4.1 概念说明

这两个过程把「一字节数据 + 一个 ACK 位」封装成一次调用，但**数据方向相反、ACK 方向也相反**，这是 I2C 最容易混的地方，用一张表钉死：

| 主机操作 | 8 个数据位谁驱动 SDA | 第 9 拍 ACK 谁驱动 SDA | 主机过程做什么 | 关键参数 |
|---|---|---|---|---|
| 写一字节 `SendByte` | **主机** | **从机** | 发 8 位 → 释放 SDA → 出第 9 拍读回 ACK 比对 | `ExpectedAck`（校验从机应答） |
| 读一字节 `ExpectByte` | **从机** | **主机** | 释放 SDA → 出 8 拍逐位读回并比对 → 出第 9 拍由主机驱动 ACK | `AckOutput`（主机驱动的应答） |

一句话：**写的时候主机校验 ACK（`ExpectedAck`），读的时候主机发出 ACK（`AckOutput`）。** 两者默认值都是 `'0'`（ACK）。读多字节时，习惯上**最后一字节用 NACK（`AckOutput='1'`）**告诉从机「别再发了，我要发 STOP 了」。

#### 4.4.2 核心流程

`I2cMasterSendByte(Data, ...)`：

```
把 Data 转 8 位 slv（<0 用 to_signed，>=0 用 to_unsigned）
SendByteInclClock(DataSlv)        -- 主机发 8 位
Sda <= 'Z'                        -- 释放，让从机驱动 ACK
CheckBitInclClock(ExpectedAck)    -- 出第 9 拍，校验从机 ACK
```

`I2cMasterExpectByte(ExpData, ...)`：

```
把 ExpData 转 8 位 slv
Sda <= 'Z'                        -- 释放，让从机驱动数据
for i in 7 downto 0 loop
    CheckBitInclClock(Data_v(i))  -- 主机出时钟，逐位读回并比对期望值
end loop;
SendBitInclClock(AckOutput)       -- 主机驱动 ACK/NACK（第 9 拍）
```

注意 `ExpectByte` 复用的是 4.1 的 `CheckBitInclClock`（读+出时钟）和 `SendBitInclClock`（写 ACK+出时钟）——同一对位原语，只是「读 8 位 + 写 1 位 ACK」的方向组合。

#### 4.4.3 源码精读

`I2cMasterSendByte` 见 [hdl/psi_tb_i2c_pkg.vhd:L527-L544](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L527-L544)：

```vhdl
if Data < 0 then  DataSlv_v := std_logic_vector(to_signed(Data, 8));
else              DataSlv_v := std_logic_vector(to_unsigned(Data, 8));  end if;
SendByteInclClock(DataSlv_v, Scl, Sda, (Prefix, "I2cMasterSendByte", Msg));
Sda <= 'Z';                                                       -- 让从机驱动 ACK
CheckBitInclClock(ExpectedAck, Scl, Sda, "ACK", (Prefix, "I2cMasterSendByte", Msg));
```

`I2cMasterExpectByte` 见 [hdl/psi_tb_i2c_pkg.vhd:L547-L568](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L547-L568)：

```vhdl
Sda <= 'Z';                                                       -- 让从机驱动数据
for i in 7 downto 0 loop
    CheckBitInclClock(Data_v(i), Scl, Sda, to_string(i), (Prefix, "I2cMasterExpectByte", Msg));
end loop;
SendBitInclClock(AckOutput, Scl, Sda, "ACK", (Prefix, "I2cMasterExpectByte", Msg));  -- 主机发 ACK
```

> 读写数据位不符的报错来自 `CheckBitInclClock` 的 `LevelCheck`，例如读回的第 5 位与期望不符会打印
> `###ERROR###: - I2cMasterExpectByte - Received wrong data [5] - <你的 Msg>`。

#### 4.4.4 代码实践（阅读型）

1. **目标**：确认「写校验 ACK、读驱动 ACK」的方向差异。
2. **步骤**：对照 testbench 的「Single Byte Read, ACK/NACK」与「Single Byte Write, ACK/NACK」两组用例：读见 [testbench/psi_tb_i2c_pkg_tb.vhd:L84-L98](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L84-L98)、写见 [testbench/psi_tb_i2c_pkg_tb.vhd:L109-L123](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L109-L123)。
3. **现象**：读用例里最后字节带 `'1'`（NACK）的是「读末字节」约定；写用例里 `'1'` 出现在 `I2cMasterSendByte` 的 `ExpectedAck` 位置（期望从机 NACK）。
4. **预期**：能指出同一个 `'1'` 在 `ExpectByte`（`AckOutput`，主机驱动 NACK）与 `SendByte`（`ExpectedAck`，主机期望从机 NACK）里**方向相反**。

#### 4.4.5 小练习与答案

- **练习 1**：读两个字节时，为什么第一字节 `AckOutput='0'`、第二字节 `AckOutput='1'`？
  - **答案**：第一字节回 ACK 告诉从机「继续发」；最后一字节回 NACK 告诉从机「停止发送」，随后主机发 STOP。这是 I2C 读事务的标准结束约定。
- **练习 2**：`I2cMasterSendByte` 的 `Data` 参数范围是 `integer range -128 to 255`，为什么能同时接受负数和超过 127 的正数？
  - **答案**：它统一映射到一个 8 位字节——负数走 `to_signed(Data, 8)`（补码），非负数走 `to_unsigned(Data, 8)`。范围 `-128..255` 恰好无重叠地覆盖 8 位字节的所有 256 种取值。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成规格要求的「**写 1 字节后 Repeated Start 读 1 字节**」混合事务，并分别验证 ACK 匹配（通过）与 ACK 不匹配（报错）两条路径。

> 重要前提：主机过程是自检的，**必须**有一个从机进程成对配合，否则主机在第一个 ACK 拍就会因 SDA 被 `'H'` 占据而报错。所以下面要同时写 `p_master` 和 `p_slave` 两个并发进程。这正是 [u7-l4](u7-l4-i2c-testbench-walkthrough.md) 会详细剖析的「双进程对拍」结构，这里先用最短版本。

### 5.1 最小 testbench 模板（示例代码）

下面是基于 `psi_tb_i2c_pkg_tb` 改写的最小骨架（**示例代码**，非仓库原有文件）：

```vhdl
-- 示例代码：演示「写 1 字节 -> Repeated Start -> 读 1 字节」的主机调用顺序
architecture sim of my_i2c_master_demo is
    signal scl : std_logic := 'H';
    signal sda : std_logic := 'H';
begin
    I2cPullup(scl, sda);                       -- 上拉常驻

    p_master : process is
    begin
        I2cBusFree(scl, sda);                  -- 本进程释放驱动
        I2cSetFrequency(400.0e3);              -- 400 kHz
        wait for 1 us;

        -- 写阶段：写地址(0x13,W) + 写数据 0x67，期望从机 ACK
        I2cMasterSendStart (scl, sda, "M: start");
        I2cMasterSendAddr  (16#13#, false, scl, sda, "M: addr W", 7);          -- ExpectedAck 默认 '0'
        I2cMasterSendByte  (16#67#, scl, sda, "M: wr 0x67");                   -- ExpectedAck 默认 '0'

        -- 切读阶段：Repeated Start + 同地址(R) + 读 0x89，末字节 NACK
        I2cMasterSendRepeatedStart(scl, sda, "M: rstart");
        I2cMasterSendAddr  (16#13#, true,  scl, sda, "M: addr R", 7);
        I2cMasterExpectByte(16#89#, scl, sda, "M: rd 0x89", '1');             -- AckOutput='1' NACK

        I2cMasterSendStop(scl, sda, "M: stop");
        wait;
    end process;

    -- 对拍的从机进程（镜像主机的每一步预期）
    p_slave : process is
    begin
        I2cBusFree(scl, sda);
        I2cSlaveWaitStart (scl, sda, "S: wait start");
        I2cSlaveExpectAddr(16#13#, false, scl, sda, "S: addr W", 7);          -- 回 ACK
        I2cSlaveExpectByte(16#67#, scl, sda, "S: expect 0x67");               -- 回 ACK
        I2cSlaveWaitRepeatedStart(scl, sda, "S: wait rstart");
        I2cSlaveExpectAddr(16#13#, true,  scl, sda, "S: addr R", 7);
        I2cSlaveSendByte  (16#89#, scl, sda, "S: send 0x89", '1');            -- 期望主机 NACK
        I2cSlaveWaitStop  (scl, sda, "S: wait stop");
        wait;
    end process;
end sim;
```

> 说明：从机过程 `I2cSlaveExpectAddr/ExpectByte/SendByte/WaitStart/WaitRepeatedStart/WaitStop` 是 [u7-l3](u7-l3-i2c-slave-and-clock-stretching.md) 的内容，这里你只需把它当作「与主机镜像对拍的对端」直接照抄即可；本讲只考核主机侧的调用顺序与 ACK 语义。

### 5.2 操作步骤

1. **跑通基线**：直接运行仓库自带的 [testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd)（它已包含完全相同的「1 Byte Write, Then 1 Byte Read」用例，见 [L137-L146](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L137-L146) 主机侧与 [L244-L251](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L244-L251) 从机侧）。用 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）编译运行，按 [u1-l3](u1-l3-simulation-and-ci.md) 的流程确认 Transcript 出现 `SIMULATIONS COMPLETED SUCCESSFULLY` 且**没有** `###ERROR###`。
2. **制造 ACK 不匹配（写阶段）**：复制该用例，把**从机** `I2cSlaveExpectByte(16#67#, ...)` 的 `AckOutput` 改成 `'1'`（从机对写入数据回 NACK），而**主机** `I2cMasterSendByte(16#67#, ...)` 保持默认 `ExpectedAck='0'`。重新仿真。
   - **预期现象**：Transcript 出现 `###ERROR###: - I2cMasterSendByte - Received wrong data [ACK] - M: data-write`。
3. **制造读数据不符（读阶段）**：把从机 `I2cSlaveSendByte(16#89#, ...)` 改成发送 `16#88#`，主机仍 `I2cMasterExpectByte(16#89#, ...)`。
   - **预期现象**：Transcript 出现 `###ERROR###: - I2cMasterExpectByte - Received wrong data [0] - M: data-read`（最低位不符）。
4. **恢复匹配并把末字节 ACK 改 NACK 双向**：主机 `ExpectByte(..., '1')`、从机 `SendByte(..., '1')` 同时 NACK，确认**不报错**——验证「NACK 本身不是错，主从期望一致才是关键」。

### 5.3 预期结果与判定

- 基线与第 4 步：无 `###ERROR###`，CI 判通过。
- 第 2、3 步：各出现一行 `###ERROR###`，CI 判失败（退出码 255，见 [u1-l3](u1-l3-simulation-and-ci.md)）。
- 若你尚无仿真环境，相关运行结果标注为「待本地验证」；但消息文本与触发条件均可由本讲源码精读直接推出，不依赖运行。

---

## 6. 本讲小结

- 主机事务的概念根基是 `...InclClock` 原语：**主机自己产生 SCL 脉冲**（`Scl <= 'Z'` 上升、`Scl <= '0'` 下降），从机侧的 `...ExclClock` 则等外部时钟——这是主从的本质区别。
- `SendBitInclClock` 一个比特 = 「建立(¼T) + SCL 高(½T) + 回低中点(¼T)」，并在 SCL 高段做回读校验（仲裁）与稳定性检查；`CheckBitInclClock` 同构，但比对的是**期望值**，所以主机「读」也是自检的。
- START / Repeated START / STOP 都是「SCL 为高时翻转 SDA」；`SendRepeatedStart` 的特别之处是它要先把可能处于低电平的 SCL/SDA 卷回高，再发 START 下降沿，因此可在一字节后续接而不发 STOP。
- `I2cMasterSendAddr` 支持 7b/10b；`ExpectedAck` 三态：`'0'`=要 ACK、`'1'`=要 NACK、其它=不校验（由 `LevelCheck` 入口短路实现）。
- 写与读的 ACK 方向相反：**写时主机用 `ExpectedAck` 校验从机应答，读时主机用 `AckOutput` 主动发出应答**；读末字节惯例用 NACK。
- 所有主机过程全程自检、失败按统一前缀打印 `###ERROR###: - <Func> - <General> - <User Msg>`，与 CI 的 `run_check_errors "###ERROR###"` 直接挂钩；且因 `severity error` 不中断仿真，主从任何一处不匹配都会留下证据。

---

## 7. 下一步学习建议

- 下一讲 [u7-l3 I2C 从机事务与时钟拉伸](u7-l3-i2c-slave-and-clock-stretching.md)：精读 `I2cSlaveWaitStart/WaitRepeatedStart/WaitStop`、`I2cSlaveExpectAddr/ExpectByte`、`I2cSlaveSendByte`，重点理解 `Timeout` 与 `ClkStretch`（时钟拉伸）参数，以及它们如何与本讲主机的 `LevelWait` 等待配合。
- 之后 [u7-l4](u7-l4-i2c-testbench-walkthrough.md) 会逐段剖析 `psi_tb_i2c_pkg_tb`，把本讲的主机过程与从机过程在 `p_master`/`p_slave` 双进程里的「成对对拍」讲透——届时可回头检验本讲综合实践里你手写的最小对拍结构。
- 想加深对底层 `LevelCheck` / `CheckLastActivity` 的理解，可复习 [u3-l1](u3-l1-compare-basic.md) 与 [u4-l1](u4-l1-activity-check.md)；想理解消息前缀与 CI 判定的契约，可复习 [u1-l3](u1-l3-simulation-and-ci.md)。
