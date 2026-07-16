# 片上调试器与 JTAG

## 1. 本讲目标

本讲聚焦 Nyuzi 的片上调试器（On-Chip Debugger, OCD）与 JTAG（IEEE 1149.1）接口，回答一个问题：

> 外部宿主机怎样通过区区五根 JTAG 信号线，把一个正在跑程序的 GPGPU 核停下来、查看它内部的寄存器和内存、改完再让它继续？

学完本讲你应该能够：

1. 说清 JTAG TAP 控制器的状态机、指令寄存器（IR）与数据寄存器（DR）的工作机制，以及 Nyuzi 为简化设计在时钟域上做的取巧处理。
2. 掌握「指令注入」这一核心思想：调试器不直接改架构状态，而是往被停核的流水线里塞一条真实的 Nyuzi 机器指令，借指令本身的能力去读寄存器/内存。
3. 理解 `CR_JTAG_DATA` 这条控制寄存器如何充当「宿主机 ⟷ 目标核」之间的双向信箱，以及 halt 的精确语义。
4. 了解本调试器尚处于实验阶段（work in progress）的一系列已知限制——尤其是多周期指令与同步访存上的限制，避免在实操中踩坑。

## 2. 前置知识

本讲是专家层（advanced）内容，建立在以下前置讲义之上，相关结论不再重复：

- **u2-l4 分支调用与控制寄存器**：`getcr`/`setcr` 是 M 格式的 `MEM_CONTROL_REG` 访存指令，读写控制寄存器编号。本讲会反复用到「注入一条 `getcr s5, 18` / `setcr s7, 18`」。
- **u3-l1 顶层 nyuzi 与系统连接**：顶层 `nyuzi.sv` 如何实例化多核与 `on_chip_debugger`。
- **u3-l2 单核流水线总览**：取指（`ifetch_tag_stage`→`ifetch_data_stage`）→ 解码 → 线程选择 → 操作数 → 执行 → 写回的各级顺序，以及「分支/回滚在写回级仲裁」的结论。
- **u7-l3 Trap 处理与回滚**：`wb_rollback_en` 是全流水线唯一的回滚仲裁输出。

下面补充两个本讲专有的背景概念。

**JTAG（Joint Test Action Group，IEEE 1149.1）** 本是为「边界扫描测试」诞生的串行协议：用一个外部时钟 `TCK`、一根模式选择 `TMS`、一根串行输入 `TDI`、一根串行输出 `TDO`（外加可选的异步复位 `TRSTn`），通过移位寄存器访问芯片内部任意长度的寄存器。它的核心机制是「先选一个寄存器，再串行移位数据」。处理器厂商后来把 JTAG 复用为调试通道（如 ARM 的 SWD/JTAG-DP），Nyuzi 也是这么做的。

**TAP（Test Access Port）状态机**：JTAG 用一个 16 状态的有限状态机决定「现在该读 IR 还是 DR、该捕获、移位还是更新」。状态机的转移只由 `TMS` 在 `TCK` 边沿的电平决定。理解这个状态机是理解一切 JTAG 操作的前提。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hardware/core/jtag_tap_controller.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/jtag_tap_controller.sv) | JTAG TAP 状态机，解析 `TCK/TMS/TDI`，输出 `capture_dr/shift_dr/update_dr/update_ir` 控制信号与当前 IR 值。是「串行 ↔ 并行」的翻译层。 |
| [hardware/core/on_chip_debugger.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv) | OCD 主体。实例化 TAP，定义 8 条 JTAG 指令（IR），用移位寄存器实现 halt 控制、指令注入、数据搬运、状态查询。 |
| [hardware/core/nyuzi.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv) | 顶层。实例化 `on_chip_debugger`，把注入信号扇出到被选核，把各核的 `injected_complete/rollback` 归并、把被选核的 `CR_JTAG_DATA` 选回调试器。 |
| [hardware/core/ifetch_data_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv) | 注入发生地。halt 时跳过正常取指，把 `ocd_inject_inst` 当作「取出的指令」送入解码级，并打上 `injected` 标记。 |
| [hardware/core/instruction_decode_stage.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv) | 把 `ifd_inst_injected` 标记带进解码结果；halt 期间屏蔽中断，保证注入指令不被打断。 |
| [hardware/core/control_registers.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv) | 维护 `CR_JTAG_DATA`（编号 18）。这是宿主机与目标核之间唯一的 32 位数据通道。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 定义 `jtag_interface` 与 `CR_JTAG_DATA = 18`。 |
| [hardware/core/config.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh) | JTAG IDCODE 的三段编码（版本/型号/厂商）。 |
| [tests/jtag-debug/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py) | JTAG 调试的唯一端到端测试。用 Verilator 模型 + 一个仿真 JTAG 宿主机（`sim_jtag`）走 socket，验证 IDCODE、bypass、注入、回滚等。 |

## 4. 核心概念与源码讲解

### 4.1 JTAG TAP 控制器

#### 4.1.1 概念说明

`jtag_tap_controller` 是 OCD 与外部世界之间的「翻译层」。它把五根慢速串行的 JTAG 信号（`TCK/TMS/TDI/TDO/TRSTn`）翻译成芯片内部用得上的并行控制信号：

- `capture_dr`：在「捕获数据寄存器」状态，把当前 DR 内容并行载入移位寄存器（让宿主机等会儿能把它移出来）。
- `shift_dr`：每个 `TCK` 周期把 DR 移一位，`TDI` 进、`TDO` 出。
- `update_dr`：在「更新数据寄存器」状态，把移位寄存器的内容并行写回 DR（让芯片真正生效）。
- `update_ir`：更新指令寄存器（IR）——选定接下来操作哪一条 DR。
- `jtag_instruction[3:0]`：当前 IR 的值，告诉 OCD「宿主机现在想访问哪条 JTAG 指令对应的寄存器」。

JTAG 的精髓是「**IR 选路，DR 传数**」：IR 是一个 4 位寄存器（Nyuzi 的指令宽度），它本身也是一个可移位的 DR；写完 IR 后，随后的 DR 访问就作用于该 IR 指定的那条数据寄存器。

#### 4.1.2 核心流程

Nyuzi 的 TAP 控制器实现了完整的 IEEE 1149.1 16 状态机，分两条对称的「支路」：

- **DR 支路**：`SELECT_DR_SCAN → CAPTURE_DR → SHIFT_DR → EXIT1_DR → (PAUSE_DR → EXIT2_DR →) UPDATE_DR → IDLE`。
- **IR 支路**：`SELECT_IR_SCAN → CAPTURE_IR → SHIFT_IR → EXIT1_IR → (PAUSE_IR → EXIT2_IR →) UPDATE_IR → IDLE`（或回 `SELECT_DR_SCAN`）。
- 另有 `RESET`（`TMS=1` 持续 5 拍到达）与 `IDLE`。

一次典型的「读 IDCODE」流程是：

```
1. 走 IR 支路：在 SHIFT_IR 状态把 4'b0000（=INST_IDCODE）移进 IR
2. UPDATE_IR 后 IR 生效，默认 DR 切换为 IDCODE 寄存器
3. 走 DR 支路：CAPTURE_DR 把 IDCODE 常量载入移位寄存器
4. SHIFT_DR 32 拍：32 位 IDCODE 经 TDO 逐位移出
```

**一个关键的简化设计**：Nyuzi 不为 JTAG 单独开一个 `TCK` 时钟域。文件头注释明确说明：它在系统主时钟上升沿采样 `TCK/TDI/TMS`，靠「检测两次采样间 `TCK` 是否跳变」来识别 JTAG 时钟边沿。

> [hardware/core/jtag_tap_controller.sv:25-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/jtag_tap_controller.sv#L25-L33) 说明：`TCK` 不是独立时钟域，而是被当作普通数据信号在系统时钟上采样。这样省掉了异步跨时钟域逻辑，但代价是 JTAG 频率必须远低于系统时钟（约 1/8 以下）。

#### 4.1.3 源码精读

状态枚举把 16 个标准状态全部列出：

> [hardware/core/jtag_tap_controller.sv:52-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/jtag_tap_controller.sv#L52-L69) 定义 `jtag_state_t`，DR 与 IR 两条支路各 7~8 个状态，外加 `RESET/IDLE`。

`state_nxt` 是一段纯组合的 `unique case`，完全由「当前状态 + `tms_sync`」决定下一状态——这正是 IEEE 1149.1 附录里的标准 TAP 状态转移图。

> [hardware/core/jtag_tap_controller.sv:81-169](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/jtag_tap_controller.sv#L81-L169) 状态转移组合逻辑。注意每个分支只看 `tms_sync`，符合 JTAG「转移只由 TMS 决定」的规则。

异步信号的同步与边沿检测：

> [hardware/core/jtag_tap_controller.sv:172-183](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/jtag_tap_controller.sv#L172-L183) `synchronizer` 先把 `TCK/TMS/TDI/TRSTn` 同步到系统时钟域；随后 `tck_rising_edge`/`tck_falling_edge` 用「上一拍 `last_tck` 与本拍 `tck_sync` 异或」检测上升/下降沿。`capture_dr/shift_dr/update_dr/update_ir` 全部 = 「对应状态 ∧ `tck_rising_edge`」，即每个 JTAG 动作严格对齐 `TCK` 上升沿。

时序与输出寄存器：

> [hardware/core/jtag_tap_controller.sv:185-214](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/jtag_tap_controller.sv#L185-L214) `state_ff` 在 `TCK` 上升沿更新；`SHIFT_IR` 时把 `tdi_sync` 移进 `jtag_instruction`；`TDO` 在 `TCK` 下降沿更新（标准要求下降沿驱动、上升沿采样），移 IR 时输出 `jtag_instruction[0]`，否则输出上层给的 `data_shift_val`（DR 的最低位）。

JTAG 物理接口定义在 defines.svh，方向由 `modport` 区分宿主/目标：

> [hardware/core/defines.svh:471-480](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L471-L480) `jtag_interface` 含 `tck/trst_n/tdi/tdo/tms` 五根线；`target` modport 声明 OCD 为输入 `tck/trst_n/tdi/tms`、输出 `tdo`，与上面时序逻辑的方向一致。

#### 4.1.4 代码实践

**实践目标**：验证 TAP 能正确返回 IDCODE，并亲手核对 IDCODE 的位拼接。

**操作步骤**：

1. 打开 [tests/jtag-debug/runtest.py:37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py#L37)，记下 `EXPECTED_IDCODE = 0x4d20dffb`。
2. 打开 [hardware/core/config.svh:55-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L55-L57)，取三段常量：`VERSION=4`、`PART_NUMBER=0xd20d`、`MANUFACTURER_ID={4'b1111,7'b1111101}`。
3. 按 `on_chip_debugger.sv` 第 77–82 行的拼接顺序手算 IDCODE。

**需要观察的现象 / 预期结果**：

IDCODE 的 32 位布局为（MSB 在前）：

\[
\text{IDCODE} =
\underbrace{0100}_{\text{version}[31{:}28]}
\;\underbrace{1101\,0010\,0000\,1101}_{\text{part}[27{:}12]}
\;\underbrace{111\,1111\,1101}_{\text{mfg}[11{:}1]}
\;\underbrace{1}_{\text{LSB}}
= \texttt{0x4d20dffb}
\]

你手算的结果应与 `EXPECTED_IDCODE` 完全一致。其中厂商 ID 的最高 4 位 `1111` 是 JEP-106 的「连续位」约定，最低位固定为 1 是 IDCODE 的标志位。

> 注：完整运行该测试需要先按 u1-l2 搭好 Verilator 环境，命令为在 `tests/jtag-debug/` 下执行 `python3 runtest.py jtag_idcode`（仅 verilator 目标）。若本地尚未构建 `nyuzi_vsim`，则上述为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：JTAG 要求「下降沿驱动 `TDO`、上升沿采样」。Nyuzi 的实现里 `TDO` 在哪个时钟沿更新？为什么这样做安全？

> **答案**：`TDO` 在 `TCK` 下降沿（`tck_falling_edge`）更新（见 `jtag_tap_controller.sv:211-212`）。因为 `TCK` 已被同步成系统时钟域内的数据信号，宿主机下一个 `TCK` 上升沿（同样被本模块在系统时钟上采样识别）才去读 `TDO`，中间隔了若干系统时钟周期，满足建立/保持时间。

**练习 2**：复位后默认 IR 值是多少？为什么 `jtag_idcode` 测试里第一次 `INST_SAME`（不移位 IR）就能读到 IDCODE？

> **答案**：`JTAG_RESET` 状态会把 `jtag_instruction <= '0`（即 `INST_IDCODE`，见 `jtag_tap_controller.sv:201-202`）。所以复位后默认选中的就是 IDCODE 寄存器，无需显式移位 IR 即可读 IDCODE——这与 IEEE 1149.1「复位后默认指令为 IDCODE/BYPASS」的约定一致。

---

### 4.2 指令注入：读寄存器与内存的核心机制

#### 4.2.1 概念说明

OCD 的精髓藏在文件头注释里的一句话：

> This works by allowing the host to inject instructions into a core's execution pipeline.（[on_chip_debugger.sv:26-30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L26-L30)）

也就是说：**调试器并不直接读写架构寄存器或内存，而是把一条真实的 Nyuzi 机器指令塞进被停核的取指级，让流水线自己「执行」出结果。** 想读内存？注入一条 `load_32`。想做计算？注入一条 `add_i`。想读控制寄存器？注入一条 `getcr`。调试器复用了 ISA 全部能力，自身几乎不需要「懂」架构状态。

那么数据怎么在「宿主机」和「被停核」之间往返？答案是一条专用控制寄存器 **`CR_JTAG_DATA`（编号 18）**。它是一个 32 位的双向信箱，是两端唯一的 32 位数据通道。

#### 4.2.2 核心流程

`on_chip_debugger` 定义了 8 条 JTAG 指令（IR 值），其中与调试直接相关的有四条：

| IR 值 | 指令 | 作用 |
|------|------|------|
| 0 | `INST_IDCODE` | 返回 32 位 IDCODE |
| 3 | `INST_CONTROL` | 7 位 DR：`{core, thread, halt}`，选择目标核/线程并控制停机 |
| 4 | `INST_INJECT_INST` | 32 位 DR = 一条机器指令，`update_dr` 时注入被选核流水线 |
| 5 | `INST_TRANSFER_DATA` | 32 位 DR = 与 `CR_JTAG_DATA` 之间的双向搬运 |
| 6 | `INST_STATUS` | 2 位 DR = 注入指令的执行状态（READY/ISSUED/ROLLED_BACK） |
| 15 | `INST_BYPASS` | 单位 DR，标准旁路 |

一次「**读目标内存一个字**」的完整往返协议（宿主机视角）：

```
1. INST_CONTROL(7b) ← halt=1, 选定 core/thread          # 停下被选核
2. INST_TRANSFER_DATA(32b) ← 地址值                     # 宿主机 → CR_JTAG_DATA = 地址
3. INST_INJECT_INST(32b) ← getcr s0, 18                 # 注入：s0 = CR_JTAG_DATA = 地址
4. INST_INJECT_INST(32b) ← load_32 s0, (s0)             # 注入：s0 = mem[地址]   ←真正的内存读
5. INST_INJECT_INST(32b) ← setcr s0, 18                 # 注入：CR_JTAG_DATA = 读到的值
6. INST_STATUS(2b) → 0 (READY)                          # 确认注入完成
7. INST_TRANSFER_DATA(32b) → 读回值                     # CR_JTAG_DATA → 宿主机
```

关键在于 `CR_JTAG_DATA` 同时被「宿主机的 `TRANSFER_DATA`」和「目标核的 `getcr/setcr`」读写，构成环形信箱：宿主机把数据写进去、注入指令把它读出来运算、再把结果写回去、宿主机再读出来。

#### 4.2.3 源码精读

8 条 JTAG 指令的 IR 编码：

> [hardware/core/on_chip_debugger.sv:96-105](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L96-L105) `jtag_instruction_t` 枚举。注意 `INST_INJECT_INST=4`、`INST_TRANSFER_DATA=5`、`INST_STATUS=6` 正是上面协议用到的三条。

`data_shift_reg` 是 OCD 的核心 32 位移位寄存器，所有 DR 操作都经过它。它按 JTAG 三拍工作：

> [hardware/core/on_chip_debugger.sv:161-188](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L161-L188) `capture_dr` 时按当前 IR 把要回送的内容（IDCODE / control / `data_to_host` / status）载入；`shift_dr` 时按位宽把它右移、`TDI` 补进最高位；`update_dr` 时若 IR=`INST_INJECT_INST`，则把移位结果锁存进 `ocd_inject_inst`。

注入的「扳机」是 `update_dr`：

> [hardware/core/on_chip_debugger.sv:129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L129) `ocd_inject_en = update_dr && jtag_instruction == INST_INJECT_INST;`——只有 DR 更新且 IR 为注入指令时，才向被选核发出一个周期的注入脉冲。

宿主机 → 目标核的数据通道（`TRANSFER_DATA` 触发）：

> [hardware/core/on_chip_debugger.sv:155-159](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L155-L159) `ocd_data_from_host = data_shift_reg`；`ocd_data_update = update_dr && IR==INST_TRANSFER_DATA`。这一对信号直接驱动 `CR_JTAG_DATA` 的写入（见下方控制寄存器）。

注入状态机（让宿主机知道注入的指令到底跑完没有）：

> [hardware/core/on_chip_debugger.sv:140-152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L140-L152) `machine_inst_status` 三态：注入时置 `ISSUED`；收到 `injected_complete`（写回级正常退休）置回 `READY`；收到 `injected_rollback`（写回级回滚，如缓存缺失）置 `ROLLED_BACK`。宿主机通过 `INST_STATUS` 读它来决定「是否需要重发」。

被选核与状态归并（顶层 `nyuzi.sv`）：

> [hardware/core/nyuzi.sv:102-113](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L102-L113) 实例化 `on_chip_debugger`；`injected_complete`/`injected_rollback` 是各核对应信号的按位或（任意一核完成即反馈）；多核时 `data_to_host = cr_data_to_host[ocd_core]`，把被选核的 `CR_JTAG_DATA` 选回调试器。

`core_selected_debug` —— 只有被 OCD 选中的核才真正接收注入：

> [hardware/core/core.sv:393](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L393) `assign core_selected_debug = CORE_ID == ocd_core;`
> [hardware/core/core.sv:395-399](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/core.sv#L395-L399) 当被注入指令在写回级退休时，`injected_complete = wb_inst_injected & !wb_rollback_en`；若被回滚则 `injected_rollback = wb_inst_injected & wb_rollback_en`。这就是上节 status 的来源。

注入在取指级落地——这是整条链路的「魔法点」：

> [hardware/core/ifetch_data_stage.sv:170-172](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L170-L172) halt 期间，`ifd_instruction = ocd_halt_latched ? ocd_inject_inst : <真实取出的指令>`。也就是说，注入指令被直接「冒充」成从 I-Cache 取出的指令，进入后续解码级。
> [hardware/core/ifetch_data_stage.sv:208-218](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L208-L218) 当 `ocd_halt` 且本核被选中时，`ifd_instruction_valid <= ocd_inject_en && core_selected_debug`，并把 `ifd_inst_injected <= 1` 打上「我是注入指令」的标记。线程号也强制切到 `ocd_thread`。

`injected` 标记随指令一路流到写回级，用来判断「这条退休的指令是不是注入的那条」：

> [hardware/core/instruction_decode_stage.sv:270](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L270) `decoded_instr_nxt.injected = ifd_inst_injected;`
> [hardware/core/writeback_stage.sv:245-255](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L245-L255) 写回级按指令来自哪条执行路径（整数/访存/浮点）取对应的 `.injected` 字段，汇聚成 `wb_inst_injected`。

最后是双向信箱 `CR_JTAG_DATA` 本身——它有 **两个写源 + 两个读源**：

> [hardware/core/control_registers.sv:120](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L120) `assign cr_data_to_host = jtag_data;` —— 宿主机 `TRANSFER_DATA` 的 capture 读它。
> [hardware/core/control_registers.sv:216-217](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L216-L217) `ocd_data_update` 时 `jtag_data <= ocd_data_from_host;` —— **宿主机写**（不经任何指令）。
> [hardware/core/control_registers.sv:207](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L207) `CR_JTAG_DATA: jtag_data <= dd_creg_write_val;` —— **目标核 `setcr` 写**。
> [hardware/core/control_registers.sv:302](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L302) `CR_JTAG_DATA: cr_creg_read_val <= jtag_data;` —— **目标核 `getcr` 读**。
> [hardware/core/defines.svh:184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L184) `CR_JTAG_DATA = 5'd18;`

这就是为什么协议里 `getcr s0, 18` / `setcr s0, 18` 的 `18` 恰好是 `CR_JTAG_DATA`：注入指令通过它从信箱取数/存数。

#### 4.2.4 代码实践

**实践目标**：阅读测试，确认「注入 getcr/setcr 经 `CR_JTAG_DATA` 往返」与「注入 load 触发缓存缺失导致回滚」两种行为。

**操作步骤**：

1. 打开 [tests/jtag-debug/runtest.py:328-384](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py#L328-L384)（`jtag_inject` 测试）。注意这一段：
   - 先 `INST_CONTROL(7b, 0x1)` 停核 0 的线程 0；
   - `TRANSFER_DATA` 写入 `0x3b643e9a`，再 `INJECT_INST 0xac0000b2`（注释 `getcr s5, 18`）→ 把刚写入的信箱值读进 s5；
   - 切到线程 1 重复，注入 `getcr s6, 18`；
   - 回到线程 0 注入 `xor s7, s5, s6`（`0xc03300e5`）和 `setcr s7, 18`（`0x8c0000f2`）把异或结果写回信箱；
   - `TRANSFER_DATA` 读回，`expect_data(0xeab81e39)` —— 这正是 `0x3b643e9a ^ 0xd1dc20a3`。

2. 打开 [tests/jtag-debug/runtest.py:386-409](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py#L386-L409)（`jtag_inject_rollback` 测试）。它注入一条 `load_32 s0, (s0)`（`0xa8000000`），地址 `0x10000` 是「未缓存的高地址」，必定触发缓存缺失 → 回滚。

**需要观察的现象 / 预期结果**：

- `jtag_inject` 中，异或结果 `0xeab81e39` 被宿主机经 `CR_JTAG_DATA` 读回，证明 `getcr/setcr` 与 `TRANSFER_DATA` 共享同一个 `jtag_data` 寄存器，闭环成立。
- `jtag_inject_rollback` 中，注入 `load_32` 后 `INST_STATUS` 返回 `STATUS_ROLLED_BACK(2)` 而非 `READY`——这正是 `machine_inst_status` 在收到 `injected_rollback` 后的状态，说明注入指令遇到缓存缺失被回滚，宿主机必须重发。

**待本地验证**：以上是源码阅读型实践。若已构建 Verilator 模型，可在 `tests/jtag-debug/` 下运行 `python3 runtest.py jtag_inject jtag_inject_rollback` 观察上述断言通过。

#### 4.2.5 小练习与答案

**练习 1**：为什么「读内存」必须先注入 `getcr s0, 18` 把地址装进 s0，而不能让 `load_32` 直接用立即数？

> **答案**：Nyuzi 的 `load_32` 是 M 格式，地址 = 基址寄存器 + 符号扩展偏移，不能直接装载任意 32 位绝对地址。宿主机只能通过 `CR_JTAG_DATA` 这个信箱把 32 位地址送进核内，再注入 `getcr s0, 18` 把信箱值取到寄存器 s0，最后 `load_32 s0, (s0)` 以 s0 为基址访存。

**练习 2**：注入指令在第 4.2.3 节里被「冒充」成取出的指令。那它会被记分牌、会被当作普通指令走完整流水线吗？这意味着什么？

> **答案**：是的。注入指令和普通指令走完全相同的解码→操作数→执行→写回路径，唯一的差别是 `injected` 标记位和 halt 期间强制选定的线程号。这意味着注入的 `load` 一样可能缓存缺失、一样会被回滚——这正是 `INST_STATUS` 与 `ROLLED_BACK` 状态存在的理由，也是「读内存」比「读寄存器」更脆弱的根源。

---

### 4.3 halt 机制

#### 4.3.1 概念说明

注入指令有个前提：**被注入的核必须停下来**，否则它的正常取指会和注入指令争抢取指级，行为无法预测。OCD 用 `INST_CONTROL` 这条 JTAG 指令的 `halt` 位来停机。

注意文件头对 halt 语义的明确刻画（[on_chip_debugger.sv:44-48](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L44-L48)）：halt 只是「停止取新指令」，已经在流水线或在指令 FIFO 里的指令仍会继续跑完。因此 halt **不是瞬时冻结**，也**无法在异常发生的那一刻立即停下**。

#### 4.3.2 核心流程

halt 信号 `ocd_halt` 来自 `control` 寄存器的最低位，经顶层扇出到所有核的取指级。被停核的行为：

1. **取指级停止取新指令**：取指仲裁器的「可取线程位图」在 halt 时被清空，正常线程不再发起新取指。
2. **取指数据级改走注入通路**：`ifd_instruction` 多路选择器切到 `ocd_inject_inst`，线程号强制为 `ocd_thread`。
3. **中断被屏蔽**：halt 期间不允许中断替换注入指令，保证注入序列原子。
4. **写回级反馈**：注入指令退休时，经 `wb_inst_injected` 反馈 `injected_complete`，宿主机据此推进下一条注入。

#### 4.3.3 源码精读

`control` 是一个 7 位 DR，含核号、线程号、halt 位：

> [hardware/core/on_chip_debugger.sv:90-94](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L90-L94) `debug_control_t {core, thread, halt}`。
> [hardware/core/on_chip_debugger.sv:120-122](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L120-L122) `ocd_halt = control.halt`、`ocd_thread = control.thread`、`ocd_core = control.core`。
> [hardware/core/on_chip_debugger.sv:140-141](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L140-L141) `update_dr && IR==INST_CONTROL` 时把移位结果载入 `control`。

取指级对 halt 的两处响应：

> [hardware/core/ifetch_tag_stage.sv:136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L136) 「可取线程」判定里含 `&& !ocd_halt`——halt 时不挑任何线程取指。
> [hardware/core/ifetch_tag_stage.sv:169](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_tag_stage.sv#L169) `pc_to_fetch = next_program_counter[ocd_halt ? ocd_thread : selected_thread_idx];`——halt 时强行把取指 PC 指向被选线程，配合注入使用。

取指数据级改走注入通路（已在 4.2.3 引用，这里强调 halt 作用）：

> [hardware/core/ifetch_data_stage.sv:180](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L180) `ifd_thread_idx <= ocd_halt ? ocd_thread : ift_thread_idx;` halt 时把指令归属线程强制为 `ocd_thread`，注入指令的副作用（如写 s0）就落在目标线程上。

中断屏蔽——halt 期间不让中断替换注入指令：

> [hardware/core/instruction_decode_stage.sv:281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/instruction_decode_stage.sv#L281) `assign raise_interrupt = masked_interrupt_flags[ifd_thread_idx] && !ocd_halt;` 只要 halt 拉高，解码级绝不产生中断替换，注入序列不被打断。

#### 4.3.4 代码实践

**实践目标**：确认 `INST_CONTROL` 的位宽与 halt 编码，并理解「先停后注」的强制顺序。

**操作步骤**：

1. 看 [tests/jtag-debug/runtest.py:328-384](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py#L328-L384) 中每次注入前都先调 `fixture.jtag_transfer(INST_CONTROL, 7, 0x1)` 或 `0x3`。
2. 解码这两个值：7 位 DR `{core[6:?], thread, halt}`，最低位是 halt。`0x1` = halt=1、thread=0、core=0；`0x3` = halt=1、thread=1、core=0（在默认 `THREADS_PER_CORE=4` 下 thread 占 2 位）。

**需要观察的现象 / 预期结果**：

- 任何 `INJECT_INST` 之前必有 `INST_CONTROL` 拉高 halt，否则注入无效（取指级不会切到注入通路）。
- 切换目标线程时（如 `0x1`→`0x3`）只需再发一次 `INST_CONTROL`，无需重新停机——halt 位保持 1。

**待本地验证**：可尝试在测试里故意省略 `INST_CONTROL`，观察注入是否仍能完成（预期：注入指令不会进入流水线，`INST_STATUS` 不返回 `READY`）。

#### 4.3.5 小练习与答案

**练习**：halt 期间，已经在流水线里的旧指令会怎样？这对「读寄存器」的准确性有何影响？

> **答案**：根据 [on_chip_debugger.sv:44-48](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L44-L48)，halt 不冲刷流水线，旧指令仍会跑完并写回寄存器。因此刚 halt 时读到的寄存器值可能还包含「停机前最后几条指令尚未退休的副作用」。实践中需要在 halt 后插入若干空操作或查询 `INST_STATUS`，等流水线排空再读，才能拿到稳定快照。

---

### 4.4 已知限制

#### 4.4.1 概念说明

文件头开宗明义：**「This is experimental and a work in progress.」**（[on_chip_debugger.sv:22-24](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L22-L24)）。理解这些限制和理解它的能力同样重要——它们直接决定了哪些调试场景可用、哪些会触发未定义行为。

#### 4.4.2 核心流程：七条限制

`on_chip_debugger.sv` 文件头列出的限制（[on_chip_debugger.sv:38-55](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L38-L55)）可归为三类：

**A. 注入可靠性**
1. 注入指令若被回滚（如缓存缺失）**不会自动重发**，宿主机必须查 `INST_STATUS` 自己重发。
2. 若指令注入时目标核的指令队列已满，注入指令**会被丢弃且无法检测**。

**B. halt 的不精确性**
3. halt 只停新取指，已进入流水线/队列的指令仍会跑完。
4. 因此**无法在异常发生瞬间立即 halt**。
5. **不支持单步**（single stepping）。
6. 不支持「监控模式」（处理器必须停下才能调试）。

**C. 不可中断指令的危险**
7. 在「成对的两段式指令」（同步访存 `load_sync`/`store_sync`、I/O 内存搬运）**两次发射之间** halt，会把处理器置于坏状态。
8. 在**多周期指令**（scatter/gather，需 16 个 subcycle）执行期间 halt，**行为未定义**。

#### 4.4.3 源码精读

限制清单的权威来源就是文件头注释：

> [hardware/core/on_chip_debugger.sv:38-55](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L38-L55) 逐条列出限制。

限制 1（回滚不自动重发）由测试 `jtag_inject_rollback` 证实——注入指令遇到缓存缺失后 `INST_STATUS` 返回 `ROLLED_BACK`，宿主机必须自行重发：

> [tests/jtag-debug/runtest.py:386-409](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py#L386-L409) 注释写道「I put in an instruction that will miss the cache, so I know it will roll back」，并断言状态为 `STATUS_ROLLED_BACK`。

「读写 PC」功能因 issue #128 被整体禁用，是「限制导致特性不可用」的实例：

> [tests/jtag-debug/runtest.py:411-412](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py#L411-L412) `@test_harness.disable` 装饰器 + 注释 `# XXX currently disabled because of issue #128`。

回滚检测在硬件侧的实现（即限制 1 的硬件基础）：

> [hardware/core/on_chip_debugger.sv:146-151](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L146-L151) `injected_rollback` 优先于 `injected_complete`，置 `ROLLED_BACK`。注意它**只报告**回滚，**不重发**——重发责任完全在宿主机软件。

#### 4.4.4 代码实践

**实践目标**：把「读内存」协议与已知限制对照，找出协议在哪些情况下会失败。

**操作步骤**：

1. 回顾 4.2.2 节的「读内存」七步协议。
2. 对照 [on_chip_debugger.sv:38-55](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L38-L55) 的限制清单，逐条标记协议里哪一步可能踩到哪条限制。

**需要观察的现象 / 预期结果**（应得出如下分析）：

- 第 4 步 `load_32 s0, (s0)` 若地址未缓存 → 触发缓存缺失 → 指令回滚 → `INST_STATUS` 返回 `ROLLED_BACK`（限制 1）。宿主机**必须**循环重发，直到 `READY`。
- 如果要读的目标地址来自一个 `scatter`/`gather` 写入的区域，且 halt 时机不当 → 命中限制 8（多周期指令期间 halt 行为未定义）。
- 如果目标核正好夹在 `load_sync`/`store_sync` 两段之间 halt → 命中限制 7，可能进入坏状态。

**结论**：本调试器「读寄存器」基本可靠（getcr/setcr 不访存、不缓存缺失）；但「读内存」必须靠 `INST_STATUS` 轮询 + 重发来对抗限制 1，且要避开限制 7/8 描述的两类不可中断指令窗口。

#### 4.4.5 小练习与答案

**练习**：为什么本调试器无法实现传统的「单步执行（single step）」？结合 halt 语义说明。

> **答案**：单步要求「执行一条指令后精确停下」。但本调试器的 halt 只是「停止取新指令」，既不冲刷流水线（限制 3），也无法在指令边界瞬时冻结（限制 4），更没有硬件单步逻辑（限制 5）。注入模型本身是「停下后塞指令」，而不是「跑一条就停」，所以无法直接表达单步语义。要近似单步，只能用注入 `call`/分支改 PC 的方式手工模拟（正是被禁用的 `jtag_read_write_pc` 试图做的，见 issue #128）。

## 5. 综合实践

**任务**：手工模拟一次「用 JTAG 读目标核内存地址 `0x1000` 处的一个 32 位字」，并写出每一步应发送的 JTAG 指令与预期回读值，同时标注每步可能触发的限制。

要求：

1. 列出宿主机依次发送的 `INST_CONTROL / INST_TRANSFER_DATA / INST_INJECT_INST / INST_STATUS` 序列，写出注入指令的助记符（`getcr`/`load_32`/`setcr`）。
2. 标注哪些步骤后必须查询 `INST_STATUS` 并在 `ROLLED_BACK` 时重发。
3. 说明：如果目标核此刻正在执行一条 `scatter` 访存的中途，这个调试流程为什么不可靠（对应哪条限制）。

**参考解答**：

```
1. INST_CONTROL(7b) ← halt=1, thread=0, core=0          # 停机（注意已停核可能在跑旧指令）
2. INST_TRANSFER_DATA(32b) ← 0x1000                      # CR_JTAG_DATA = 地址
3. INST_INJECT_INST(32b) ← getcr s0, 18                  # s0 = 0x1000
4. INST_INJECT_INST(32b) ← load_32 s0, (s0)              # s0 = mem[0x1000]
   INST_STATUS(2b) → 轮询，若 ROLLED_BACK 则回到第 4 步重发   # ← 限制 1（缓存缺失）
5. INST_INJECT_INST(32b) ← setcr s0, 18                  # CR_JTAG_DATA = 读到的值
   INST_STATUS(2b) → READY
6. INST_TRANSFER_DATA(32b) → 读回 mem[0x1000]            # 宿主机拿到结果
7. INST_CONTROL(7b) ← halt=0                             # 恢复运行
```

若目标核正卡在一条 `scatter` 访存的 16 个 subcycle 中途，halt 会命中 [on_chip_debugger.sv:53-55](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/on_chip_debugger.sv#L53-L55) 的「多周期指令期间 halt 行为未定义」，整个读内存流程不可靠——这是本调试器作为实验性设施的根本性约束。

## 6. 本讲小结

- Nyuzi 用标准 JTAG（IEEE 1149.1）作为调试通道，`jtag_tap_controller` 实现完整 16 状态 TAP 状态机，并取巧地把 `TCK` 当作系统时钟域内的数据信号采样（而非独立时钟域），换取实现简化、代价是 JTAG 频率受限。
- 调试的核心思想是**指令注入**：OCD 往被停核的取指级塞一条真实机器指令，复用 ISA 自身能力去读寄存器/内存；注入指令靠 `injected` 标记在写回级被识别，靠 `INST_STATUS` 报告 READY/ISSUED/ROLLED_BACK。
- `CR_JTAG_DATA`（编号 18）是宿主机与目标核之间唯一的 32 位双向信箱：宿主机经 `INST_TRANSFER_DATA` 直写、目标核经 `getcr/setcr 18` 读写，构成读寄存器/内存的闭环数据通道。
- **halt 不是瞬时冻结**：它只停新取指、不冲刷流水线、不支持单步、不能在异常瞬间停下，因此读快照需等流水线排空。
- 本调试器仍是实验性（work in progress）：注入指令回滚需宿主机自行重发；在 `load_sync/store_sync` 两段之间或 `scatter/gather` 多周期指令期间 halt 会触发未定义行为。
- 端到端验证由 [tests/jtag-debug/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/jtag-debug/runtest.py) 完成，它驱动 Verilator 模型与一个仿真 JTAG 宿主机（`sim_jtag`）经 socket 通信，覆盖 IDCODE、注入、回滚等场景。

## 7. 下一步学习建议

- **u11-l3 GDB 远程调试与 LLDB**：本讲讲的是硬件级 JTAG 通道；下一讲会讲模拟器侧用 `remote-gdb.c` 实现 GDB 远程串行协议，提供源码级断点/单步——可对比「硬件 JTAG 注入调试」与「模拟器 GDB 调试」在能力与限制上的差异（前者无单步、后者在虚拟内存启用时受限）。
- **u11-l2 性能计数器与 profiling**：同样经由控制寄存器访问，但目的是观测而非控制，可对比两类控制寄存器用途。
- **继续阅读源码**：若想验证本讲的注入链路，建议跟踪一条注入指令从 [ifetch_data_stage.sv:170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/ifetch_data_stage.sv#L170) 一路流到 [writeback_stage.sv:245](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/writeback_stage.sv#L245)，亲手确认 `injected` 标记如何变成 `injected_complete`。同时可阅读 `hardware/testbench/sim_jtag.sv` 了解测试用的仿真 JTAG 宿主机如何用 DPI-C socket 把 Python 命令翻译成 TCK/TMS/TDI 序列。
