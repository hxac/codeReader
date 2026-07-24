# 通用原语：仲裁器、复用器、防抖、Gearbox、PRBS

## 1. 本讲目标

本讲聚焦 `base/general/rtl/` 里被全仓库高频复用的一组「积木级」原语。读完本讲你应该能够：

- 看懂 `Arbiter` 的轮询仲裁逻辑，并会用 `ArbiterPkg` 的 `priorityEncode` / `arbitrate` 自行实现公平选择；
- 理解 `Debouncer`（按键防抖）、`WatchDogRst`（看门狗复位）、`PwrUpRst`（上电复位保持）这三种「时间驱动」原语各自解决什么问题；
- 理解 `Gearbox` 用一个可变写指针的移位寄存器做任意位宽↔位宽转换的思路；
- 会用 `PrbsPkg`（以及更常用的 `StdRtlPkg.lfsrShift`）生成伪随机数据，作为测试激励或链路自检码型。

这些原语是后续 AXI-Stream（u4）、SSI（u5）等数据平面模块的底层零件。本讲承接 u1-l5 的双进程风格（`RegType` / `REG_INIT_C` / `r` / `rin` / `comb` / `seq`）和 u1-l4 的 `StdRtlPkg` 约定。

## 2. 前置知识

- **双进程风格**：SURF 的时序逻辑统一写成 `comb`（算次态 `v`，结尾 `rin <= v`）+ `seq`（打寄存器 `r <= rin after TPD_G`）两段，详见 u1-l5。
- **复位三泛型**：`TPD_G`（仿真延迟）、`RST_POLARITY_G`（复位有效电平）、`RST_ASYNC_G`（复位同步还是异步），三者让复位逻辑与极性解耦，详见 u1-l4。
- **`StdRtlPkg` 工具函数**：本讲会用到 `bitSize(n)`（表示 n 所需位数）、`bitReverse`、`wordCount`、`getTimeRatio`、`uOr`、`decode` 等，它们都定义在 `base/general/rtl/StdRtlPkg.vhd`。
- **LFSR 直觉**：伪随机序列（PRBS）由线性反馈移位寄存器产生——把寄存器里若干「抽头（tap）」位异或后塞回最低位、整体移位，周而复始就得到一个看似随机、实则确定可复现的比特流。这是本讲 PRBS 与 Gearbox 之外所有「随机/扰码」模块的共同底座。

## 3. 本讲源码地图

| 文件 | 角色 | 关键点 |
|------|------|--------|
| [base/general/rtl/Arbiter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd) | 轮询仲裁器实体 | 记住上次选中的请求者，只要它还保持请求就继续服务它，否则推进到下一个 |
| [base/general/rtl/ArbiterPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/ArbiterPkg.vhd) | 仲裁算法包 | `priorityEncode` 函数 + `arbitrate` 过程，是 Arbiter 的「大脑」 |
| [base/general/rtl/Mux.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Mux.vhd) | 通用 N:1 选择器 | `SEL_WIDTH_G` 决定 `2**N:1`，三级可选流水（输入/选择/输出） |
| [base/general/rtl/Debouncer.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd) | 按键防抖 | 先同步、再按 `DEBOUNCE_PERIOD_G` 计时滤除毛刺 |
| [base/general/rtl/WatchDogRst.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/WatchDogRst.vhd) | 看门狗复位 | 监测信号长时间不跳变就拉复位 |
| [base/general/rtl/PwrUpRst.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/PwrUpRst.vhd) | 上电复位保持 | 上电后把复位保持 `DURATION_G` 拍再释放 |
| [base/general/rtl/Gearbox.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Gearbox.vhd) | 位宽转换 | 用一个变宽移位寄存器在 `SLAVE_WIDTH_G` 与 `MASTER_WIDTH_G` 间转换 |
| [base/general/rtl/PrbsPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/PrbsPkg.vhd) | 伪随机函数包 | 显式抽头数的 LFSR 单步函数（`getPrbs1xTap`…`getPrbs4xTap`）等 |

> 提示：这一目录由 [base/general/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/ruckus.tcl) 用 `loadSource -lib surf -dir ".../rtl"` 整目录纳入构建，所以新增通用原语只需丢进 `rtl/` 即可被全仓库使用（详见 u1-l2）。

---

## 4. 核心概念与源码讲解

### 4.1 仲裁器（Arbiter）与选择原语

#### 4.1.1 概念说明

当一个共享资源（一条总线、一个内存端口、一个 AXI-Stream 主机口）被多个请求者争用时，需要仲裁器决定「这一拍服务谁」。SURF 的 `Arbiter` 采用**轮询（round-robin）**策略：

- 记住「上一次选中谁」（`lastSelected`）；
- 只要上一位请求者**还保持请求**，就继续把资源留给它（**保持 / hold** 行为，避免同一拍反复切换）；
- 一旦它撤销请求，就从「上一位的下一个」开始找下一个有请求的位（**轮询**，避免低编号或高编号请求者被饿死）。

与 `Arbiter` 形成对照的是 [Mux.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Mux.vhd)：`Mux` 是一个**纯组合/可选流水**的 `2**SEL_WIDTH_G : 1` 选择器，由外部 `sel` 决定选谁，自身没有「公平」概念。简单说：`Mux` 选谁由你说了算，`Arbiter` 选谁由「谁在请求 + 上次选了谁」共同决定。

#### 4.1.2 核心流程

`Arbiter` 的 `comb` 进程每拍做这样一件事（伪代码）：

```text
如果 (上次选中的请求者 已撤销请求) 或 (本就没有有效选中):
    调用 arbitrate(req, lastSelected, ...) 重新仲裁
否则:
    维持上次的 ack / selected 不变     -- 这就是 hold
```

真正的「找下一个」逻辑在 `ArbiterPkg.arbitrate` / `priorityEncode` 里：

```text
arbitrate(req, lastSelected):
    valid := (req 中至少有一个 1)           -- uOr(req)
    if valid:
        pivot := (lastSelected + 1) mod N    -- 从「下一位」开始找
        next  := priorityEncode(req, pivot)  -- 从 pivot 起向上(带回绕)找第一个 1
        ack   := 只有第 next 位为 1 的独热码   -- decode(next)
    else:
        next、ack 维持不变
```

#### 4.1.3 源码精读

先看实体接口。`REQ_SIZE_G` 是请求路数；`selected` 用 `bitSize(REQ_SIZE_G-1)` 位宽编码当前选中的下标，`ack` 是独热（one-hot）的授予向量：

[base/general/rtl/Arbiter.vhd:24-38](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L24-L38) —— 实体声明：`req` 输入、`selected`（二进制下标）、`valid`（有无授予）、`ack`（独热授予）。

状态记录与初值，注意 `lastSelected` 就是「记忆」：

[base/general/rtl/Arbiter.vhd:44-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L44-L56) —— `RegType` 含 `lastSelected / valid / ack`，`REG_INIT_C` 全 0。

`comb` 进程的核心一行——hold 判定：

[base/general/rtl/Arbiter.vhd:65-67](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L65-L67) —— 只有当「上次选中的那位请求已撤销」或「原本就没有有效选中」时才重新仲裁；否则维持。

仲裁算法在包里。`priorityEncode` 的做法是：先把输入向量「右旋 pivot 位」让 pivot 跑到最低位，再找最低位的 1，最后把下标加回 pivot 还原：

[base/general/rtl/ArbiterPkg.vhd:41-66](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/ArbiterPkg.vhd#L41-L66) —— `rotatedV := rotate_right(...)`，循环找最低置位下标，`ret := (bestReq + p) mod length` 还原。

> 说明：该函数注释写着「p 拥有最高优先级，其后是 p-1、p-2…」；结合 `rotate_right` + 「找最低置位」的实际效果，命中顺序是从 pivot 起向上（带回绕）的第一个 1。配合调用方传入 `pivot = (lastSelected+1) mod N`，最终表现就是标准的轮询——总是跳过刚被服务的那位、从下一位开始找。函数本身只负责「从给定 pivot 找下一个」，公平性由调用方「pivot 永远是 lastSelected+1」保证。

`arbitrate` 过程把上述函数与「有无请求」拼起来：

[base/general/rtl/ArbiterPkg.vhd:69-85](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/ArbiterPkg.vhd#L69-L85) —— `valid := uOr(req)`；有效时用 `priorityEncode` 选下一个、用 `decode` 生成独热 `ack`；无请求时维持 `lastSelected` 并清零 `ack`。

#### 4.1.4 代码实践

仓库已经为 `Arbiter` 提供了完整的 cocotb 回归测试 [tests/base/general/test_Arbiter.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/general/test_Arbiter.py)，直接运行它即可观察「轮询 + 保持」行为。

1. **实践目标**：用真实测试观察 `Arbiter` 在竞争请求下的授予顺序，理解 hold 与 round-robin。
2. **操作步骤**（在仓库根目录，假设已按 u1-l2 装好 `.venv` 与 `ruckus`）：
   ```bash
   # 先生成 cocotb 源缓存（详见 u1-l2 / u9-l1）
   make MODULES=$PWD import
   # 只跑 Arbiter 这一组
   ./.venv/bin/python -m pytest -q tests/base/general/test_Arbiter.py
   ```
3. **需要观察的现象**：阅读 [test_Arbiter.py 的 `round_robin_selection_test`](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/general/test_Arbiter.py#L119-L129)，它依次送入 `0b1110 → 0b1100 → 0b1000 → 0b0011`。请手算每一拍 `selected`/`ack` 应该是多少（参考其 Python 镜像 [`_priority_encode`](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/general/test_Arbiter.py#L40-L49)），再与测试断言对照。
4. **预期结果**：授予会沿「上次选中的下一位」轮转；`hold_current_request_test` 验证当胜出者不撤销请求时授予保持不变；`reset_behavior_test` 验证复位清零选中历史。
5. 若本机尚未配好 GHDL/cocotb 工具链，上述命令的运行结果**待本地验证**；此时可改为「源码阅读型实践」：手动模拟 `req=0b1110, lastSelected=0`，按 4.1.2 的流程推出 `pivot=1`、`next=1`、`ack=0b0010`。

#### 4.1.5 小练习与答案

**练习 1**：`REQ_SIZE_G=4`、`lastSelected=0`、`req=0b1010`（第 1、3 位请求）。求 `selected` 与 `ack`。
**答案**：`pivot=(0+1) mod 4=1`；从位 1 起向上找第一个 1 → 位 1；故 `selected=1`，`ack=0b0010`。

**练习 2**：为什么 `Arbiter` 在「胜出者保持请求」时不切换到其他等待者？这样会不会饿死别人？
**答案**：这是 hold 行为，避免同一拍内被服务的请求者被抢占、也减少选择抖动。它不会饿死别人，因为一旦该请求者撤销请求（哪怕一拍），`pivot=lastSelected+1` 就会立刻把机会让给下一位——`starvation_rotation_test` 正是用来验证这一点。

---

### 4.2 防抖与复位看护（Debouncer / WatchDogRst / PwrUpRst）

这三个原语都属于「用计数器衡量一段时间」的家族，但用途不同。

#### 4.2.1 概念说明

- **Debouncer（按键防抖）**：机械按键按下/弹起时会有几毫秒的毛刺。防抖器在检测到输入跳变后，强制等待 `DEBOUNCE_PERIOD_G` 秒，期间忽略一切跳变，等计数到 0 才让输出跟上输入。
- **WatchDogRst（看门狗复位）**：监测一个「应该周期性跳变」的信号 `monIn`（比如心跳）。一旦它超过 `DURATION_G` 拍没有活动，就认定系统卡死，拉一次复位。
- **PwrUpRst（上电复位保持）**：上电瞬间时钟和寄存器可能尚未稳定，需要把复位信号**保持** `DURATION_G` 拍后再释放，确保全芯片进入已知状态。

#### 4.2.2 核心流程

**Debouncer** 的 `comb` 进程（简化）：

```text
记录上一拍的同步输入 iSyncedDly
if (检测到 iSynced 的任意跳变):  filter := CNT_MAX_C   -- 重新装填计时
elsif (filter /= 0):              filter := filter - 1   -- 倒计时
-- filter 归零且输出与输入不一致时，才让输出 o 跟上输入
```

其中 `CNT_MAX_C` 由「防抖周期 / 时钟周期」换算得到。

**WatchDogRst** 的进程（简化）：

```text
每拍默认 rstOut 无效
if (monInput 活跃):          cnt := 0                  -- 有心跳，清计数
else:                        cnt := cnt + 1
                             if (cnt 达到 DURATION_G):  rstOut := 有效   -- 超时复位
```

**PwrUpRst** 的进程：复位同步后，用一个计数器把 `rstOut` 保持 `CNT_SIZE_C` 拍再撤销。

#### 4.2.3 源码精读

`Debouncer` 的时间换算——把「秒」换算成「时钟周期数」：

[base/general/rtl/Debouncer.vhd:42-45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L42-L45) —— `CNT_MAX_C := getTimeRatio(DEBOUNCE_PERIOD_G, CLK_PERIOD_C) - 1`。

`Debouncer` 先做可选的 2 级同步（输入通常是异步的按键），边沿触发模式还可改用 `RstSync` 检测前沿：

[base/general/rtl/Debouncer.vhd:65-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L65-L99) —— `SYNCHRONIZE_G` 控制是否串同步器；`SYNC_EDGE_TRIG_G` 切换电平/前沿模式。

防抖计时核心——任意跳变重新装填、否则递减、归零后才更新输出：

[base/general/rtl/Debouncer.vhd:108-126](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L108-L126) —— 跳变置 `CNT_MAX_C`、递减、`filter=0` 且 `o` 与输入不一致时翻转 `o`。

`WatchDogRst`——`monIn` 先经 `Synchronizer` 跨域，超时计数到 `DURATION_G` 才拉复位：

[base/general/rtl/WatchDogRst.vhd:56-77](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/WatchDogRst.vhd#L56-L77) —— 活跃则 `cnt<=0`，否则自增，`cnt=DURATION_G` 时 `rst<=OUT_POLARITY_G`。

`PwrUpRst`——先用 `RstSync` 同步外部复位，再用计数器把复位保持若干拍：

[base/general/rtl/PwrUpRst.vhd:54-82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/PwrUpRst.vhd#L54-L82) —— `cnt` 计到 `CNT_SIZE_C` 才把 `rst` 撤销；`SIM_SPEEDUP_G=true` 时仿真用 127 拍加速。

#### 4.2.4 代码实践

1. **实践目标**：通过修改 `Debouncer` 的防抖周期参数，体会 `CNT_MAX_C` 是如何随时钟频率与防抖时长缩放的。
2. **操作步骤**：
   - 阅读 [Debouncer.vhd:42-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L42-L43) 的 `CNT_MAX_C` 计算。
   - 手算：`CLK_FREQ_G=156.25e6`、`DEBOUNCE_PERIOD_G=1.0e-3` 时，`CNT_MAX_C` 约为多少？（提示：`getTimeRatio(1e-3, 1/156.25e6)-1`）
3. **需要观察的现象**：防抖周期越大、时钟越慢，`CNT_MAX_C` 越大，`filter` 字段占的位宽也越大。
4. **预期结果**：约 `156250-1 = 156249`，即需要约 18 位计数器。
5. 运行 [tests/base/general/test_Debouncer.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/general/test_Debouncer.py) 可验证真实行为；命令运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`WatchDogRst` 里 `monIn` 为什么要先过一个 `Synchronizer`？
**答案**：`monIn` 往往来自另一时钟域（如心跳计数器），直接采样会触发亚稳态；先同步到本地 `clk` 再计数，保证 `cnt` 不被毛刺误判（同步器原理见 u2-l1）。

**练习 2**：`PwrUpRst` 的 `SIM_SPEEDUP_G` 为什么要把 `CNT_SIZE_C` 换成 127？
**答案**：上电保持 `DURATION_G`（默认 1.56 亿拍）是为了真实硬件稳定，但仿真里等这么久不可接受；仿真时用 127 拍快速完成保持逻辑的覆盖，又不改变「先保持后释放」的状态机骨架。

---

### 4.3 位宽转换 Gearbox

#### 4.3.1 概念说明

很多链路的物理层用窄位宽（如 16/32 位）逐拍传输，而 FPGA 内部数据通路喜欢宽位宽（如 64/128 位）以提吞吐。`Gearbox` 就是一个**通用位宽转换器**：输入 `SLAVE_WIDTH_G` 位、输出 `MASTER_WIDTH_G` 位，二者可以任意大小关系（宽→窄、窄→宽、甚至等宽做对齐）。

它的核心思想是一根**可变有效宽度的移位寄存器**：每来一个输入字就往里「写」`SLAVE_WIDTH_G` 位，写指针随之前进；每当累计的位数够一个输出字（`MASTER_WIDTH_G` 位），就从底部「切」出一个输出字并把指针回退。

#### 4.3.2 核心流程

```text
shiftReg: 一段比"两个最大宽度之和"略大的缓冲区
writeIndex: 当前缓冲区里"已写入但尚未切出"的位数

每拍:
  1. 若上层要求 slip(调整对齐): writeIndex 回退一格 (用于 64B/66B 之类的对齐)
  2. 若 writeIndex >= MASTER_WIDTH: 从 shiftReg 底部切走一个输出字, writeIndex -= MASTER_WIDTH
  3. 若输入有效且本拍没有输出: 把 slaveData 按 writeIndex 偏移写进 shiftReg, writeIndex += SLAVE_WIDTH
  4. 若 writeIndex >= MASTER_WIDTH: 标记本拍有有效输出 masterValid
```

`SLAVE_BIT_REVERSE_G` / `MASTER_BIT_ORDER` 控制进/出端是否做比特序翻转（很多高速收发器是 LSB 先发，而 RTL 习惯 MSB 在高位）。

#### 4.3.3 源码精读

缓冲区宽度按「最大宽度向上取整 + 余量」设计，`+1` 是为 slip 预留空间：

[base/general/rtl/Gearbox.vhd:55-59](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Gearbox.vhd#L55-L59) —— `SHIFT_WIDTH_C := wordCount(MAX_C, MIN_C) * MIN_C + MIN_C + 1`。

「够一个输出字就切一刀」的逻辑：

[base/general/rtl/Gearbox.vhd:114-123](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Gearbox.vhd#L114-L123) —— `writeIndex >= MASTER_WIDTH_G` 时移出 `MASTER_WIDTH_G` 位、指针减相应位数。

接受新输入、按 `writeIndex` 偏移写入（注意源码特意把偏移拷到普通变量 `lo` 以便综合器识别「变基+常量宽」切片）：

[base/general/rtl/Gearbox.vhd:144-158](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Gearbox.vhd#L144-L158) —— 可选 `bitReverse` 输入、`shiftReg(lo+W-1 downto lo) := dataIn`、`writeIndex += SLAVE_WIDTH_G`。

输出端可选比特序翻转：

[base/general/rtl/Gearbox.vhd:170-175](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Gearbox.vhd#L170-L175) —— `masterBitOrder='1'` 时输出 `bitReverse(shiftReg(MASTER_WIDTH_G-1 downto 0))`。

> 同步版 `Gearbox` 处理同频不同位宽；若两侧还跨时钟域，则用同目录的 [AsyncGearbox.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/AsyncGearbox.vhd)，它在 Gearbox 之外再加异步 FIFO（原理见 u2-l2）。AXI-Stream 版本 `AxiStreamGearbox` 见 u4-l3。

#### 4.3.4 代码实践

1. **实践目标**：用一个 `2:1` 窄→宽 Gearbox，把 2 个 16 位字拼成 1 个 32 位字。
2. **操作步骤**：
   - 阅读并运行 [tests/base/general/test_Gearbox.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/general/test_Gearbox.py)，找出它实例化 `Gearbox` 时用的 `SLAVE_WIDTH_G`/`MASTER_WIDTH_G` 组合。
   - 在脑中/纸上跟踪：连送两拍 `slaveData=0xABCD`、`0x1234`，观察第几拍 `masterValid` 拉高、`masterData` 是多少。
3. **需要观察的现象**：每积累满 `MASTER_WIDTH_G` 位才输出一次；输出频率 ≈ 输入频率 × `SLAVE_WIDTH_G / MASTER_WIDTH_G`。
4. **预期结果**：窄→宽时输出有效是「间歇」的（每两拍一拍有效）；具体拼字顺序取决于 `SLAVE_BIT_REVERSE_G` 与写入偏移，**待本地验证**确切片序。
5. 若无工具链，可做源码阅读型实践：对照 4.3.2 的伪代码，手算 `writeIndex` 在每拍的取值序列。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SHIFT_WIDTH_C` 里要 `+1`？
**答案**：因为 `slip` 功能会把 `writeIndex` 临时回退一格，缓冲区需要多留 1 位以防写指针回退后与尚未切出的数据重叠越界（源码注释亦点明「Don't need the +1 if slip is not used」）。

**练习 2**：宽→窄（如 32→16）转换时，`masterValid` 的行为和窄→宽有何不同？
**答案**：宽→窄时每接受一个输入字就能切出不止一个输出字，可能出现连续多拍 `masterValid='1'`；而窄→宽是多拍输入才凑出一拍输出。`comb` 里「切一刀后若仍够再切」的 `if (v.writeIndex >= MASTER_WIDTH_G)` 正是为了处理宽→窄的多次切出。

---

### 4.4 伪随机序列 PRBS（PrbsPkg 与 lfsrShift）

#### 4.4.1 概念说明

PRBS（Pseudo-Random Binary Sequence，伪随机二进制序列）由 LFSR 产生：一拍一拍地移位，并把若干「抽头」位异或后塞回，得到一串看似随机、实则确定可复现的比特流。它在 SURF 里主要有两类用途：

1. **测试激励 / 链路自检码型**：发端用 PRBS 填充 payload，收端用同一多项式同步推演并比对，即可检验整条数据通路有无丢拍、错位、错码（见 u5-l2 的 `SsiPrbsTx/Rx`、u4-l4 的 `AxiStreamPrbsFlowCtrl`）。
2. **扰码/解扰**：用 LFSR 打乱数据使其近似随机分布，便于接收端做时钟恢复（同目录的 `Scrambler` 即此用途）。

SURF 提供两层 PRBS 工具：

- **底层显式抽头函数**：`PrbsPkg` 提供 `getPrbs1xTap`…`getPrbs4xTap`（抽头数固定为 1~4 个）、`getGaloisPrbs4xTap`（Galois 型）、`getXorRand`。这些是「一步移位」的纯函数，抽头位置由参数指定。
- **上层灵活 LFSR**：`StdRtlPkg.lfsrShift(lfsr, taps)` 接受**任意长度**的 `NaturalArray` 抽头表，是仓库里真正高频使用的版本（`SsiPrbsTx/Rx`、`AxiStreamPrbsFlowCtrl` 都调它）。

> 准确性提示：经全仓库检索，`PrbsPkg` 里的 `getPrbs*xTap` 目前没有被其他模块直接调用，它们更像「显式、教学化」的 LFSR 单步原语；真正在协议核里产生 PRBS 码型的是 `StdRtlPkg.lfsrShift`。本讲两者都介绍，避免混淆。

#### 4.4.2 核心流程

Fibonacci 型 LFSR 单步（`getPrbsNxTap` 的思路）：

```text
newLfsr(i)   := lfsr(i+1)            -- 整体左移, 最低位腾空
newLfsr(最高位) := lfsr(0) xor lfsr(tap0) [xor ...更多抽头]   -- 反馈填回
```

`lfsrShift` 的思路一致，只是抽头数可变、且会判断向量方向（升/降序）决定移位方向，还带一个 `input` 位用于扰码器把数据异或进反馈。

#### 4.4.3 源码精读

`PrbsPkg` 接口——一组抽头数固定的单步函数：

[base/general/rtl/PrbsPkg.vhd:25-34](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/PrbsPkg.vhd#L25-L34) —— `getPrbs1xTap`…`getPrbs4xTap`、`getGaloisPrbs4xTap`、`getXorRand`。

典型 Fibonacci 单步：移位 + 异或反馈：

[base/general/rtl/PrbsPkg.vhd:39-57](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/PrbsPkg.vhd#L39-L57) —— `retVar(input'left) := input(0) xor input(tap0)`。

仓库实际使用的 `lfsrShift`（在 `StdRtlPkg`，不在 `PrbsPkg`）：

[base/general/rtl/StdRtlPkg.vhd:1105-1120](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L1105-L1120) —— 按向量方向移位，对 `taps` 数组里每个抽头异或到最低位，返回新 LFSR 值。

调用例：`SsiPrbsTx` 每产生一拍随机 payload 就推进一步 LFSR：

[protocols/ssi/rtl/SsiPrbsTx.vhd:290](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd#L290) —— `v.randomData := lfsrShift(v.randomData, PRBS_TAPS_G, '0');`。

#### 4.4.4 代码实践

1. **实践目标**：用一个固定种子和固定抽头表，手工推演一段 LFSR 序列，体会「确定性伪随机」。
2. **操作步骤**：
   - 取一个 7 位 LFSR、种子 `0b0000001`、抽头表 `{6,5}`（即 PRBS7 常用抽头 7,6 在 0 基下标下为 6,5）。
   - 按 `lfsrShift` 的规则手算前 8 拍的值（整体移位、把 `lfsr(6) xor lfsr(5)` 填回最低位）。
3. **需要观察的现象**：序列看似无规律，但只要种子和抽头相同，每次推演结果完全一致。
4. **预期结果**：会得到一串周期为 \(2^7-1=127\) 的不重复码型（除全 0 态外）。具体前 8 拍数值**待本地验证**。
5. 进阶（源码阅读型）：在 [SsiPrbsTx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsTx.vhd) 与 [SsiPrbsRx.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPrbsRx.vhd) 中追踪 `lfsrShift` 的两次调用，理解「发端推进 LFSR 产出 payload、收端用同一 LFSR 同步推演并比对」的环回校验原理（详见 u5-l2）。

PRBS 的周期与抽头选择满足本原多项式时达到最大周期：

\[
\text{周期}_{\max} = 2^{n} - 1
\]

其中 \(n\) 为 LFSR 级数；只有当抽头对应的多项式为本原多项式时才能取到该最大周期。

#### 4.4.5 小练习与答案

**练习 1**：为什么 PRBS 称为「伪」随机？
**答案**：因为它完全由「种子 + 抽头多项式」决定，确定且可复现，并不具备真随机的不可预测性；但它的统计特性（0/1 均衡、游程分布）接近随机，足以用作测试码型。

**练习 2**：`getPrbsNxTap` 与 `lfsrShift` 的本质区别是什么？
**答案**：`getPrbsNxTap` 把抽头数写死在函数名里（1~4 个抽头各一个函数），抽头位置靠参数；`lfsrShift` 用 `NaturalArray` 接受任意个数抽头，更通用，且额外带 `input` 位可直接做扰码器，所以协议核统一用它。

---

## 5. 综合实践

把本讲几个原语串成一个迷你「PRBS 驱动的轮询仲裁」观察实验：

1. **场景**：4 个请求源，每个源是否发出请求由各自的一段 PRBS（用 `lfsrShift` 产生）决定；用一个 `Arbiter(REQ_SIZE_G => 4)` 仲裁；观察 `ack` 的授予顺序。
2. **步骤**：
   - 用 `lfsrShift` 写一个简化的请求发生器（示例代码，非项目原有文件）：4 个独立 LFSR，每拍取其最低位拼成 `req(3 downto 0)` 喂给 `Arbiter`。
     ```vhdl
     -- 示例代码：仅示意，不在仓库中
     r.req(0) <= lfsrShift(r.lfsr0, PRBS_TAPS_C)(0);  -- 其余 3 路同理
     ```
   - 在 `Arbiter` 的 `comb` 里临时加一行 `report` 把 `selected` 打印出来（仿真用），或直接复用 [test_Arbiter.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/general/test_Arbiter.py) 的 `TB` 把 `req` 改成 PRBS 驱动。
3. **观察要点**：
   - 当某个请求源被授予后只要它仍保持请求（PRBS 连续为 1），授予就**保持**在它身上（验证 4.1 的 hold）；
   - 一旦它撤销，授予立即跳到「下一位」开始找（验证轮询、无饿死）；
   - 把多拍 `selected` 记下来，统计每个源被授予的次数，应大致均匀（PRBS 的均衡性带来公平性）。
4. **预期结果**：授予序列既体现 hold（连续服务同一源），又体现 round-robin（切换时跳到下一位）；长期看 4 个源被服务次数接近。完整波形**待本地验证**。

> 这个综合实验把「仲裁器（4.1）」与「PRBS（4.4）」结合；若想再加入位宽转换，可把仲裁后选中的请求编号经一个 `Gearbox`（4.3）打包成宽字输出，体会三种原语的协作。

## 6. 本讲小结

- `Arbiter` + `ArbiterPkg` 用「记住上次选中者 + 从下一位起找」实现**轮询仲裁**，并通过 hold 判定避免无谓切换；`Mux` 则是无状态的选择器。
- `Debouncer` / `WatchDogRst` / `PwrUpRst` 都是「计数器计时」原语，分别解决按键毛刺、系统卡死、上电稳定三类时间问题。
- `Gearbox` 用一根可变有效宽度的移位寄存器 + 写指针，在任意 `SLAVE_WIDTH_G ↔ MASTER_WIDTH_G` 间做位宽转换，并支持 slip 对齐与比特序翻转。
- PRBS 由 LFSR 产生；`PrbsPkg` 是显式抽头的单步函数，而仓库实际高频使用的是 `StdRtlPkg.lfsrShift`，二者共同支撑测试码型与扰码。
- 这些原语都遵循 u1-l5 的双进程骨架与 u1-l4 的命名/复位约定，是后续 AXI-Stream、SSI 等数据平面模块的底层零件。

## 7. 下一步学习建议

- **位宽转换的进阶**：本讲的同步 `Gearbox` 在 u4-l3 会升级为 `AxiStreamGearbox`（带 AXI-Stream 握手与打包/解包）；跨时钟域版本 `AsyncGearbox` 可结合 u2-l2 的异步 FIFO 一起读。
- **PRBS 的真实用途**：u5-l2 会讲 `SsiPrbsTx/Rx` 如何用 `lfsrShift` 做帧级环回自检，u4-l4 会讲 `AxiStreamPrbsFlowCtrl` 用 PRBS 做流控压力测试——届时回看本讲 4.4 会更有体感。
- **仲裁的扩展**：在 u4-l3 的 `AxiStreamMux` 里会看到「带 AXI-Stream 握手的仲裁」如何复用本讲的轮询思想，并加上帧边界保护。
- **测试方法**：若你想更系统地运行本讲引用的 cocotb 测试，可直接跳到 u9-l1 / u9-l2 学习 `run_surf_vhdl_test` 与参数扫描框架。
