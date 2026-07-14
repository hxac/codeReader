# 同步 FIFO（olo_base_fifo_sync）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清一个**标准同步 FIFO** 的端口、状态信号与 **fall-through（FWFT）** 行为；
- 理解 Open Logic 用「双电平计数器 + 单周期脉冲」实现内部状态、并采用两进程法（record）的写法；
- 解释 **几乎满（AlmFull）/几乎空（AlmEmpty）** 等级的含义、判断方向与触发时刻；
- 解释 `ReadyRstState_g` 在「复位期间 `In_Ready` 的电平」与「写侧时序关键路径」之间的取舍；
- 读懂并运行 `olo_base_fifo_sync` 的 VUnit 测试台，理解 `test_configs` 如何用 `named_config` 覆盖多种 generic 组合。

## 2. 前置知识

本讲承接 [u2-l2 流水线阶段与 AXI-S 握手](u2-l2-pipeline-stage-handshake.md)（两进程法、record、反压）与 [u2-l3 RAM 实现](u2-l3-ram-implementations.md)（`olo_base_ram_sdp`、RBW/WBR），在此基础上把「RAM 存储 + 状态控制」组合成一个完整组件。需要先理解以下概念：

- **FIFO（First-In First-Out，先进先出队列）**：一个缓冲结构，写口按顺序压入数据，读口按相同顺序弹出，先写先出。
- **同步 FIFO（Synchronous FIFO）**：读写端口共用同一个时钟 `Clk`，不存在跨时钟域问题。
- **fall-through / FWFT（First-Word-Fall-Through，首字直通）**：只要 FIFO 非空，队头数据就**直接出现在 `Out_Data` 上**且 `Out_Valid=1`；下游拉高 `Out_Ready` 才「消费/弹出」一个字。这与「标准读 FIFO」（必须先发读命令、下一拍才出数）不同。
- **填充度（level）**：FIFO 中当前缓存的字数。
- **AXI-S 握手**：`In_Valid`/`In_Ready` 与 `Out_Valid`/`Out_Ready`，一次传输发生在 Valid 与 Ready 同时为 1 的上升沿。
- **两进程法**：组合进程 `p_comb` 只算下一拍状态 `r_next`，时序进程 `p_seq` 只打拍并复位，状态收进 record（见 u2-l2）。
- **反压（back-pressure）**：当下游不收（`Out_Ready=0`）且 FIFO 已满时，写侧必须用 `In_Ready=0` 停止接收，否则会丢数据。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/base/vhdl/olo_base_fifo_sync.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd) | 同步 FIFO 的 RTL 实现：状态机、电平计数、几乎满/空判断，并实例化 `olo_base_ram_sdp` 作为存储。 |
| [src/base/vhdl/olo_base_ram_sdp.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd) | FIFO 内部使用的简单双端口 RAM（u2-l3 已讲），提供 1 拍读延迟与 RBW/WBR 行为切换。 |
| [doc/base/olo_base_fifo_sync.md](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_sync.md) | 官方实体文档：泛型、端口、状态信号说明。 |
| [test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd) | VUnit 测试台：复位检查、读写、写满、读空、几乎标志、占空比扫描等用例。 |
| [sim/test_configs/olo_base.py](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py) | 把同一个 TB 注册成多组 generic 配置（`RamBehavior`/`ReadyRstState`/`Depth`/`AlmFull`×`AlmEmpty`）。 |
| [sim/test_configs/utils.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py) | `named_config` 辅助函数：把 generic 字典翻译成具名 VUnit 配置。 |

## 4. 核心概念与源码讲解

### 4.1 FIFO 接口与状态信号

#### 4.1.1 概念说明

`olo_base_fifo_sync` 是一个**最简同步 FIFO**：读写同钟、fall-through 输出、两侧都是标准 AXI-S 接口。它的价值不在「能存数据」（那是底层 RAM 的事），而在于**正确地维护一组状态信号**——让写侧知道「还能不能写」（`In_Ready`/`Full`），让读侧知道「有没有数可读」（`Out_Valid`/`Empty`），并提供精确的填充度（`In_Level`/`Out_Level`）。

设计要点是把存储和控制分离：真正的存储阵列由 [`olo_base_ram_sdp`](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd#L186-L200) 提供，FIFO 实体本身只负责「读写指针 + 电平计数 + 状态译码」。这也意味着 `RamBehavior_g`、`RamStyle_g` 直接透传给 RAM，复用了 u2-l3 讲过的跨器件块 RAM 适配能力。

#### 4.1.2 核心流程

FIFO 的关键难点是**电平（fill level）的正确维护**。一个朴素的实现是用「写指针 − 读指针」单一地算电平，但这样写侧和读侧会争用同一个计数器，容易成为时序瓶颈。Open Logic 采用更精巧的**双电平计数器 + 单周期脉冲**方案：

```
WrLevel : 写侧看到的电平（写立即 +1，读的反馈延迟 1 拍 -1）
RdLevel : 读侧看到的电平（读立即 -1，写的反馈延迟 1 拍 +1）
RdUp    : 「本拍发生了一次写」的单周期脉冲（写侧 → 读侧）
WrDown  : 「本拍发生了一次读」的单周期脉冲（读侧 → 写侧）
```

- **写侧逻辑**：当 `WrLevel ≠ Depth` 且 `In_Valid=1` 时允许写入 → 推进 `WrAddr`、置 `RdUp=1`；若本拍没有同时发生读（`WrDown=0`）则 `WrLevel+1`，否则净变化为 0。
- **读侧逻辑**：当 `RdLevel ≠ 0` 且 `Out_Ready=1` 时允许读出 → 推进 `RdAddr`、置 `WrDown=1`；若本拍没有同时发生写（`RdUp=0`）则 `RdLevel-1`，否则净变化为 0。
- **同时读写不丢不重**：同一拍既写又读时，写侧因 `WrDown=1` 不增 `WrLevel`、读侧因 `RdUp=1` 不减 `RdLevel`，净电平不变，数据直接「穿堂过」。

由于读写同钟，`RdUp`/`WrDown` 是同一时钟域内的单周期脉冲，天然安全（异步 FIFO 才需要格雷码同步，见下一单元 u3-l1）。`RdUp`/`WrDown` 被打进 record 寄存一拍，因此**对侧看到的事件有 1 拍延迟**——这正是文档里 `In_Level`/`Out_Level` 各自「本侧操作立即反映、对侧操作延迟 1 拍反映」的由来。

电平位宽用 `log2ceil(Depth_g + 1)` 位（注意 `+1`）：当 `Depth_g` 恰为 2 的幂时，`log2ceil(Depth_g)` 位只能表示 `0..Depth-1`，无法区分「满」与「空」，多 1 位才能把 `Depth` 本身表示出来。

#### 4.1.3 源码精读

实体声明把所有可选泛型都给了默认值，只强制 `Width_g`/`Depth_g`：

[olo_base_fifo_sync.vhd:33-43](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L33-L43) —— 泛型表：`AlmFullOn_g`/`AlmEmptyOn_g` 默认 `false`（不需要时可省略，省资源），`RamBehavior_g` 默认 `"RBW"`，`ReadyRstState_g` 默认 `'1'`。

[olo_base_fifo_sync.vhd:44-63](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L44-L63) —— 端口表：注意 `In_Valid` 默认 `'1'`、`Out_Ready` 默认 `'1'`，即不接反压时退化为「永远写/永远读」；`In_Level`/`Out_Level` 位宽随 `Depth_g` 由 `log2ceil` 推导。

两进程法的状态收进 record，这正是 u2-l2 讲过的写法：

[olo_base_fifo_sync.vhd:71-78](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L71-L78) —— `TwoProcess_r` 收纳 `WrLevel`/`RdLevel`/`RdUp`/`WrDown`/`WrAddr`/`RdAddr`。

写侧的状态更新与指针回绕：

[olo_base_fifo_sync.vhd:93-109](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L93-L109) —— `WrAddr` 到达 `Depth-1` 后回绕到 0（环形缓冲）；写发生时置 `RdUp`；用 `WrDown` 判断是否真的要 `WrLevel+1`（避免与同拍读相加）。

写侧状态译码（`In_Ready` 与 `Full`）：

[olo_base_fifo_sync.vhd:112-118](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L112-L118) —— `WrLevel = Depth` 时 `Full=1` 且 `In_Ready=0`；否则 `In_Ready=1`。`In_Ready` 是纯组合输出，不带寄存器。

读侧状态更新与读地址输出：

[olo_base_fifo_sync.vhd:131-145](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L131-L145) —— 读地址 `RamRdAddr <= v.RdAddr`（用**下一拍**的地址去读 RAM，配合 RAM 的 1 拍读延迟实现 fall-through）；`Out_Ready=1` 时推进 `RdAddr`、置 `WrDown`。

读侧状态译码（`Out_Valid` 与 `Empty`）：

[olo_base_fifo_sync.vhd:148-154](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L148-L154) —— `RdLevel > 0` 时 `Out_Valid=1`、`Empty=0`。这就是 fall-through 的本质：只要 FIFO 有数，`Out_Valid` 立即为 1，`Out_Data` 上就是队头字。

`In_Level`/`Out_Level` 直接取自寄存器（同步输出）：

[olo_base_fifo_sync.vhd:167-169](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L167-L169) —— `Out_Level <= r.RdLevel; In_Level <= r.WrLevel;`

复位：同步、高有效，写在 `p_seq` 末尾作覆盖，且**只清指针与电平、不清 RAM 内容**（与 u2-l3 RAM 复位约定一致）：

[olo_base_fifo_sync.vhd:171-184](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L171-L184)

存储实例化——只透传 4 个泛型给 RAM（读延迟用 `olo_base_ram_sdp` 的默认值 1）：

[olo_base_fifo_sync.vhd:186-200](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L186-L200)

#### 4.1.4 代码实践

**实践目标**：用眼睛验证 fall-through 行为与状态信号时序，不依赖综合。

**操作步骤**：

1. 打开 [olo_base_fifo_sync_tb.vhd:148-223](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L148-L223)（`TwoWordsWriteAndRead` 用例）。
2. 逐拍对照下面的时序表，理解 `In_Level`/`Out_Level`/`Empty`/`Out_Data` 是如何演化的。

| 拍次 | 动作 | In_Level | Out_Level | Empty | Out_Valid | Out_Data |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| 写 1（0x0001） | In_Valid=1 | 0→1 | 0 | 1 | 0 | - |
| 写 2（0x0002） | In_Valid=1 | 1→2 | 0 | 1 | 0 | - |
| 暂停 1 | In_Valid=0 | 2 | 0→1 | 1→0 | 0→1 | 0x0001 |
| 暂停 2 | 无操作 | 2 | 1→2 | 0 | 1 | 0x0001 |

**需要观察的现象**：第 3 拍（暂停 1）`Out_Valid` 才拉高、`Out_Data=0x0001`——这「滞后 1 拍」正是 RAM 1 拍读延迟 + `RdLevel` 跨侧反馈的体现。

**预期结果**：表中每一格都与 TB 里 `check_equal(...)` 的断言一致。若你能跑仿真，可在波形上确认；**若暂无仿真器，标记为「待本地验证」**，但时序表本身可直接从源码推导。

#### 4.1.5 小练习与答案

**练习 1**：为什么电平位宽是 `log2ceil(Depth_g+1)` 而不是 `log2ceil(Depth_g)`？

**答案**：当 `Depth_g` 是 2 的幂（如 32）时，`log2ceil(32)=5` 位只能表示 `0..31`，无法表示「满（=32）」这个值；多 1 位（`log2ceil(33)=6`）才能把 `Depth` 本身表示出来，从而区分「满」与「空」。

**练习 2**：若 `Out_Ready` 恒为 `'1'`、`In_Valid` 恒为 `'1'`，FIFO 会不会一直清空？

**答案**：取决于读写速率是否相等。两者同钟且每拍都有效时，净电平趋近 0（数据穿堂过）；但只要上游偶尔停一拍，就会累积。关键在于「满则 `In_Ready=0`、空则 `Out_Valid=0`」会自动节流，不会读出无效数据。

---

### 4.2 几乎满与几乎空等级

#### 4.2.1 概念说明

`Full`/`Empty` 只在**临界点**才翻转：满到最后一格才告诉写侧「停」，空到最后一格才告诉读侧「没数了」。这对上下游而言反应太晚——上游流水线可能已经把数据推进来、来不及刹车。**几乎满（Almost Full）/几乎空（Almost Empty）** 就是提前预警：在到达临界点**之前**若干格就翻转标志，给上下游留出反应时间。

两个等级由独立的开关与阈值控制：

- `AlmFullOn_g`（开关）+ `AlmFullLevel_g`（阈值）：到达阈值后，写侧提前收到「快满了」。
- `AlmEmptyOn_g`（开关）+ `AlmEmptyLevel_g`（阈值）：到达阈值后，读侧提前收到「快空了」。

开关默认关闭（`false`）——不需要时省掉比较器，符合「Ease of Use：一实体只做必要的事」。

#### 4.2.2 核心流程

两个标志的判断方向是**不对称**的，这与各自服务对象匹配：

- **几乎满**看**写侧电平** `WrLevel`，用 **`>=`**：写侧关心「我再写几格就满」，所以阈值是上界。

  \[
  \text{AlmFull} = (\text{WrLevel} \ge \text{AlmFullLevel\_g})
  \]

- **几乎空**看**读侧电平** `RdLevel`，用 **`<=`**：读侧关心「我再读几格就空」，所以阈值是下界。

  \[
  \text{AlmEmpty} = (\text{RdLevel} \le \text{AlmEmptyLevel\_g})
  \]

「写侧看写侧电平、读侧看读侧电平」让比较器离各自端口最近、时序最短。注意：由于 `WrLevel`/`RdLevel` 各自对对侧操作有 1 拍延迟，这两个标志是**保守的**（可能提前 1 拍翻转），绝不会「迟到」，对 FIFO 这是安全方向。

#### 4.2.3 源码精读

几乎满判断（注意是 `>=` 且基于 `r.WrLevel`）：

[olo_base_fifo_sync.vhd:124-128](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L124-L128)

几乎空判断（注意是 `<=` 且基于 `r.RdLevel`）：

[olo_base_fifo_sync.vhd:156-160](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L156-L160)

文档里对这两个标志的官方说明（含阈值含义）：

[olo_base_fifo_sync.md:80-81](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_sync.md#L80-L81) —— `AlmFull` 在 `fill level >= AlmFullLevel_g` 时置位；`AlmEmpty` 在 `fill level <= AlmEmptyLevel_g` 时置位；开关关闭时输出未定义（故 TB 里仅当 `AlmFullOn_g` 为真才检查）。

测试台把阈值设为常量，随 `Depth_g` 联动：

[olo_base_fifo_sync_tb.vhd:42-44](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L42-L44) —— `AlmFullLevel_c := Depth_g - 3; AlmEmptyLevel_c := 5;`。即默认情况下，写到还差 3 格满时 `AlmFull` 拉高，读到剩 5 格时 `AlmEmpty` 拉高。

`AlmostFlags` 用例在填充/排空过程中逐格校验标志翻转时刻：

[olo_base_fifo_sync_tb.vhd:318-331](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L318-L331) —— 填充阶段：当 `i+1 >= AlmFullLevel_c` 时期望 `AlmFull='1'`，否则 `'0'`；当 `i+1 <= AlmEmptyLevel_c` 时期望 `AlmEmpty='1'`，否则 `'0'`。

#### 4.2.4 代码实践

**实践目标**：确认 `AlmFull` 在「电平到达阈值那一格」翻转，而非到达满。

**操作步骤**：

1. 设 `Depth_g = 32`，则 `AlmFullLevel_c = 32 - 3 = 29`、`AlmEmptyLevel_c = 5`。
2. 阅读 `AlmostFlags` 用例的填充循环 [olo_base_fifo_sync_tb.vhd:309-332](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L309-L332)，推演每写入一字后 `In_Level`/`AlmFull`/`AlmEmpty` 的取值。
3. 若有仿真器，只跑这一个用例并观察波形（运行方式见 4.4.4）。

**需要观察的现象**：

- `In_Level` 从 0 增长到 `5` 之前 `AlmEmpty='1'`，越过 5 后 `AlmEmpty='0'`；
- `In_Level` 达到 `29`（即 `AlmFullLevel_c`）那一格 `AlmFull` 由 `'0'` 翻为 `'1'`，**而不是等到 32**。

**预期结果**：翻转时刻精确等于阈值。若暂无仿真器，上述结论可由源码 `>=`/`<=` 比较直接推导得出（**待本地验证**仅为波形确认）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AlmFull` 用 `>=` 而 `AlmEmpty` 用 `<=`？

**答案**：`AlmFull` 是「上界预警」（快满了），阈值是允许的最大电平，达到即报警，故 `>=`；`AlmEmpty` 是「下界预警」（快空了），阈值是允许的最小电平，跌至即报警，故 `<=`。

**练习 2**：把 `AlmFullLevel_g` 设为 0、`AlmFullOn_g` 设为 true 会怎样？

**答案**：`WrLevel >= 0` 恒成立，`AlmFull` 永远为 `'1'`——毫无预警意义。所以阈值必须是一个大于 0、小于 `Depth_g` 的合理上界。TB 里用 `Depth_g - 3` 正是典型用法。

---

### 4.3 ReadyRstState_g：复位期间的 In_Ready

#### 4.3.1 概念说明

`In_Ready` 是写侧时序上**最敏感**的信号之一：它直接决定上游能不能在本拍写入，常常位于关键路径上。Open Logic 让 `In_Ready` 保持**纯组合**（`WrLevel /= Depth`，见 4.1.3），没有额外寄存器或复位多路选择，以换取最短路径。

但「复位期间 `In_Ready` 应该是什么电平」不同系统有不同期望：

- 有的上游要求复位期间 `In_Ready=0`（表示「我现在不收」），否则可能在复位未完成时就注入数据；
- 有的上游无所谓，只要复位释放后正确即可。

`ReadyRstState_g` 就是这个旋钮：默认 `'1'`（复位期间也保持就绪，逻辑最简），可设 `'0'`（复位期间强制拉低，多一个门）。

#### 4.3.2 核心流程

`In_Ready` 的生成分两步：

1. **基础值**：`WrLevel = Depth` → `'0'`（满），否则 `'1'`。
2. **复位覆盖**：仅当 `ReadyRstState_g = '0'` 且 `Rst = '1'` 时，把 `In_Ready` 强制为 `'0'`。

关键在于 `ReadyRstState_g` 是**编译期常量**（generic）。综合器会把整个 `if (ReadyRstState_g = '0') and (Rst = '1')` 当作常量条件处理：

- `ReadyRstState_g = '1'`（默认）：条件恒假，这段代码被优化消失，`In_Ready` 路径上**没有任何与复位相关的额外逻辑**；
- `ReadyRstState_g = '0'`：条件退化为 `Rst = '1'`，`In_Ready` 多出一个「或上 `Rst` 再取反」的门（即复位期间强制低）。

于是「默认配置 = 最简时序路径」，需要保守行为时才付出一点面积/时序代价——这正是「Trustable Code + Ease of Use」的典型权衡。

#### 4.3.3 源码精读

基础 `In_Ready`/`Full` 译码：

[olo_base_fifo_sync.vhd:112-118](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L112-L118)

复位期间的覆盖（注意是 generic 常量比较）：

[olo_base_fifo_sync.vhd:120-122](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd#L120-L122) —— `if (ReadyRstState_g = '0') and (Rst = '1') then In_Ready <= '0'; end if;`

文档对该泛型的说明（写侧时序关键路径的动机）：

[olo_base_fifo_sync.md:45](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_sync.md#L45)

测试台专门检查复位期间 `In_Ready` 是否符合 generic 约定：

[olo_base_fifo_sync_tb.vhd:121-126](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L121-L126) —— 复位保持期间断言 `check_equal(toStdl(ReadyRstState_g), In_Ready, ...)`，即两种配置都要验证。

#### 4.3.4 代码实践

**实践目标**：对比两种 `ReadyRstState_g` 下复位期间 `In_Ready` 的差异。

**操作步骤**：

1. 在 `test_configs/olo_base.py` 中，FIFO 的 TB 已经被注册了 `ReadyRstState_g` 取 `0` 和 `1` 两组配置（见 [olo_base.py:82-83](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L82-L83)）。
2. 阅读复位检查段 [olo_base_fifo_sync_tb.vhd:121-126](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L121-L126)，分别推演 `ReadyRstState_g=1` 与 `=0` 时 `In_Ready` 的值。

**需要观察的现象**：`ReadyRstState_g=1` 时复位期间 `In_Ready='1'`；`=0` 时 `In_Ready='0'`。

**预期结果**：两组配置的 `Reset` 用例都应通过（`check_equal` 不报错）。波形验证**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么默认值是 `'1'` 而不是 `'0'`？

**答案**：默认 `'1'` 时，复位覆盖分支恒假、被综合优化掉，`In_Ready` 路径最简、最快；而复位期间「上游不慎写一脚」通常不会造成实际问题（复位一释放 FIFO 就是空的）。把「最简路径」作为默认，体现了对写侧时序的照顾。

**练习 2**：若上游协议要求复位期间 `In_Ready` 必须为 `'0'`，应如何配置？

**答案**：把 `ReadyRstState_g` 设为 `'0'`。此时 `In_Ready` 在 `Rst=1` 期间被强制拉低，代价是路径上多一个与 `Rst` 相关的门。

---

### 4.4 VUnit 测试台结构

#### 4.4.1 概念说明

Open Logic 为每个实体配一个 testbench（TB），这是「Trustable Code」哲学的落地（见 u1-l1）。`olo_base_fifo_sync` 的 TB 用 VUnit 框架写成，特点是：

- **一个 TB 文件、多个测试用例**：用 `run("用例名")` 区分，由 VUnit 的 `runner_cfg` 泛型驱动调度；
- **`run_all_in_same_sim`**：所有用例在**同一次仿真**里顺序执行（只 elaborate 一次），节省编译/启动开销；
- **generic 参数化**：通过 `named_config` 把同一 TB 注册成多组 generic 组合，VUnit 为每组跑一遍全部用例。

理解这套结构，你就能为新实体写出风格一致的 TB，也能在 CI 里复现 Open Logic 的验证覆盖。

#### 4.4.2 核心流程

一个 VUnit TB 的骨架是：

```
test_runner_setup(runner, runner_cfg);   -- 启动
while test_suite loop
    if run("用例A") then ... elsif run("用例B") then ... end if;
end loop;
test_runner_cleanup(runner);             -- 收尾（失败时由 check 触发）
```

- `runner_cfg`：VUnit 注入的字符串泛型，告诉 TB「这次只跑哪个用例/哪个配置」。
- `run("X")`：当本次仿真轮到用例 X 时返回 true。
- `check_equal(...)`：VUnit 的断言宏，不符即记录失败、继续跑（默认）或按配置停止。
- `test_runner_watchdog`：看门狗，防止 TB 卡死。

generic 组合则在 Python 侧注册：`test_configs/olo_base.py` 中的 `named_config(tb, {...})` 把一个泛型字典翻译成形如 `RamBehavior_g=RBW-Depth_g=32` 的具名配置，调用 VUnit 的 `tb.add_config(...)`，于是「库.TB.配置.用例」成为一个可独立运行的测试点。

#### 4.4.3 源码精读

TB 顶部标注 `run_all_in_same_sim`（同仿真跑全部用例）：

[olo_base_fifo_sync_tb.vhd:25-35](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L25-L35) —— TB 自身的泛型：`AlmFullOn_g`/`AlmEmptyOn_g` 默认 `true`、`Depth_g` 默认 32、`RamBehavior_g` 默认 `"RBW"`、`ReadyRstState_g` 默认 1，并声明 `runner_cfg`。

DUT 实例化（注意 TB 的 `ReadyRstState_g` 是 integer，用 `toStdl` 转成 std_logic 再传给 DUT）：

[olo_base_fifo_sync_tb.vhd:75-101](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L75-L101)

时钟与看门狗：

[olo_base_fifo_sync_tb.vhd:106](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L106) —— 100 MHz 自由时钟；
[olo_base_fifo_sync_tb.vhd:111](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L111) —— 10 ms 看门狗。

主控进程与用例分发骨架：

[olo_base_fifo_sync_tb.vhd:113-148](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L113-L148) —— `test_runner_setup` → `while test_suite loop` → 第一个用例 `run("Reset")`。其余用例包括 `TwoWordsWriteAndRead`、`WriteFullFifo`、`ReadEmptyFifo`、`AlmostFlags`、`DiffDutyCycle`。

Python 侧为 FIFO 注册的 generic 组合（sync 与 async 共用循环，sync 额外加一个奇数深度 53）：

[olo_base.py:76-91](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L76-L91) —— 遍历 `RamBehavior ∈ {RBW, WBR}`、`ReadyRstState ∈ {0,1}`、`Depth ∈ {32,128,53}`、`AlmFull × AlmEmpty`（4 组），逐个 `named_config`。

`named_config` 的实现（把字典拼成配置名并调用 `add_config`）：

[utils.py:15-21](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L15-L21)

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：运行 `olo_base_fifo_sync` 的测试台；分别改 `Depth_g` 与几乎满阈值各跑一次，记录 `AlmFull` 拉起时刻并验证与阈值一致。

**操作步骤**：

1. 进入仿真目录并先用默认配置跑通（默认仿真器为 GHDL，见 u1-l4）：

   ```bash
   cd sim
   python run.py --ghdl "*olo_base_fifo_sync*"
   ```

   预期：所有配置 × 所有用例全部 PASS（**具体输出待本地验证**）。

2. **改 `Depth_g`**：这是 TB 的 generic，已被 `test_configs` 注册为 `32/128/53`，所以无需改代码即可覆盖。若想再加一个值，在 [olo_base.py:84-85](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L84-L85) 的 `for Depth in [32, 128]:` 里追加一个数（如 `64`），重跑即可。

3. **改几乎满阈值**：注意 DUT 的 `AlmFullLevel_g` 在 TB 里被接到**常量** `AlmFullLevel_c`（而非 generic）：

   [olo_base_fifo_sync_tb.vhd:43](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L43) —— `constant AlmFullLevel_c : natural := Depth_g - 3;`

   所以改阈值需编辑这一行，例如改成 `Depth_g/2`，再重跑 `AlmostFlags` 用例：

   ```bash
   python run.py --ghdl "*olo_base_fifo_sync*AlmostFlags*"
   ```

**需要观察的现象**：

- 改 `Depth_g` 后，`AlmFull` 仍在「`In_Level` 到达 `Depth_g-3`」那一格拉起（阈值随深度联动）；
- 把 `AlmFullLevel_c` 改成 `Depth_g/2` 后，`AlmFull` 拉起时刻提前到「`In_Level` 到达 `Depth_g/2`」。

**预期结果**：每次修改后 `AlmostFlags` 用例的 `check_equal(AlmFull, ...)` 仍全部通过，说明翻转时刻与新阈值精确吻合。命令的精确过滤语法与输出格式**待本地验证**。

> 提示：VUnit 的测试全名为 `olo_tb.olo_base_fifo_sync_tb.<配置名>.<用例名>`，命令行可用 `*` 通配符过滤；若过滤串不命中，可先不带过滤全跑一遍，从列表里复制确切名字。

#### 4.4.5 小练习与答案

**练习 1**：`run_all_in_same_sim` 带来什么好处？什么情况下不该用？

**答案**：它让一个 TB 的全部用例在同一次仿真（一次 elaborate）里顺序执行，省去重复编译/启动开销，适合用例之间无副作用污染的场景。若某用例会留下影响后续用例的全局状态（例如修改了共享存储、未清理），则应改为每用例独立仿真。

**练习 2**：为什么 TB 里 `AlmFullLevel_g` 不做成 generic，而硬编码成常量 `AlmFullLevel_c`？

**答案**：因为 TB 的意图是「让阈值随深度联动（`Depth_g - 3`）以自动适配不同 `Depth_g` 配置」，把它写成由 `Depth_g` 派生的常量更简洁，避免在 `test_configs` 里为每个 `Depth` 单独算一个阈值再传入。代价是改阈值要改 TB 源码——这正是本讲实践中改阈值的方式。

---

## 5. 综合实践

把本讲的「接口与状态 / 几乎满空 / ReadyRstState / 测试台」串起来，做一个源码阅读 + 小实例化任务。

**任务**：在脑中（或新建一个最小 TB）实例化一个 `Depth_g=8`、`Width_g=16`、`AlmFullOn_g=true`、`AlmFullLevel_g=6`、`AlmEmptyOn_g=true`、`AlmEmptyLevel_g=1`、`ReadyRstState_g='0'` 的同步 FIFO，回答下列问题并相互印证：

1. **接口**：写出它的 `In_Level`/`Out_Level` 位宽。
2. **复位**：复位期间 `In_Ready` 是什么电平？为什么？
3. **填充**：逐字写入，第几字写入后 `AlmFull` 拉高？第几字之后 `AlmEmpty` 才掉？
4. **fall-through**：写入第 1 字后，`Out_Valid` 何时变 1？`Out_Data` 何时等于第 1 字？

**参考答案（示例代码片段供对照，非项目原有代码）**：

```vhdl
-- 示例代码：最小实例化
i_fifo : entity olo.olo_base_fifo_sync
    generic map (
        Width_g         => 16,
        Depth_g         => 8,
        AlmFullOn_g     => true,
        AlmFullLevel_g  => 6,
        AlmEmptyOn_g    => true,
        AlmEmptyLevel_g => 1,
        ReadyRstState_g => '0'
    )
    port map (
        Clk => Clk, Rst => Rst,
        In_Data => In_Data, In_Valid => In_Valid, In_Ready => In_Ready,
        Out_Data => Out_Data, Out_Valid => Out_Valid, Out_Ready => Out_Ready,
        Full => Full, Empty => Empty, AlmFull => AlmFull, AlmEmpty => AlmEmpty
    );
```

1. `log2ceil(8+1)=4` 位（0..15，可表示到 8）。
2. `In_Ready='0'`，因为 `ReadyRstState_g='0'` 且 `Rst='1'`（见 4.3.3 的覆盖分支）。
3. `AlmFull` 在 `WrLevel >= 6` 时拉高，即第 **6** 字写入后；`AlmEmpty` 在 `RdLevel <= 1` 时为 1，故写入第 **2** 字后（`RdLevel` 越过 1）`AlmEmpty` 才掉。
4. 写入第 1 字后，下一拍 `RdLevel` 变 1（`RdUp` 反馈延迟 1 拍），此时 `Out_Valid='1'`；`Out_Data` 因 RAM 1 拍读延迟，再下一拍稳定为第 1 字的值。

**验收**：把你推演的 4 个答案，对照 `olo_base_fifo_sync.vhd` 的源码与 `AlmostFlags` 用例的断言逐一核对；若能跑仿真，用上面的实例化片段搭一个最小 TB 验证波形（**待本地验证**）。

## 6. 本讲小结

- `olo_base_fifo_sync` 是一个读写同钟、fall-through（FWFT）的标准同步 FIFO，存储交给 `olo_base_ram_sdp`，自身只维护指针与状态。
- 内部用「双电平计数器（`WrLevel`/`RdLevel`）+ 单周期脉冲（`RdUp`/`WrDown`）」避免单一共享计数器成为时序瓶颈；同时读写时净电平不变，做到不丢不重。
- `Full`/`Empty` 是临界标志，`AlmFull`（`WrLevel >= 阈值`，上界预警）/`AlmEmpty`（`RdLevel <= 阈值`，下界预警）是提前预警，方向不对称、各自服务最近的端口。
- `In_Ready` 为纯组合输出以照顾时序关键路径；`ReadyRstState_g`（默认 `'1'`）是编译期常量，控制复位期间 `In_Ready` 电平，默认配置下复位覆盖逻辑被优化消失。
- VUnit TB 用 `run_all_in_same_sim` 把多用例合并到一次仿真，`test_configs` 用 `named_config` 把 generic 组合注册成具名配置，实现「一个 TB、多组配置、全面覆盖」。
- 复位只清指针与电平、不清 RAM 内容；电平位宽用 `log2ceil(Depth_g+1)` 以区分满与空。

## 7. 下一步学习建议

- 继续学 [u3-l1 异步 FIFO（olo_base_fifo_async）](u3-l1-async-fifo.md)：把本讲的电平计数推广到双时钟域，看格雷码指针如何安全跨域，以及它何时比 `olo_base_cc_handshake` 更合适。
- 学 [u3-l2 包 FIFO（olo_base_fifo_packet）](u3-l2-packet-fifo.md)：在同步 FIFO 基础上引入包边界（`Last`）与存储转发、丢包/跳过机制。
- 复习 [u2-l3 RAM 实现](u2-l3-ram-implementations.md) 中 RBW/WBR 与 `shared variable` 的写法，本讲 FIFO 的存储行为完全由那里决定。
- 若关心验证工程化，可跳到 [u10-l1 VUnit 测试台结构与验证组件](u10-l1-vunit-tb-and-vcs.md)，系统学习 VC（验证组件）与 generic 参数化用例的设计范式。
