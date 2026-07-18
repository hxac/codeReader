# 雷达模式控制器与扫描时序

> 承接 [u5-l1 PLFM Chirp 生成与发射机](u5-l1-plfm-chirp-and-transmitter.md)。u5-l1 讲了**发射端**如何用 `plfm_chirp_controller` 的状态机产出「16 长 + 16 短 = 32 chirp / 仰角」的 staggered PRI 波形。本讲把镜头转向**接收端**：接收链也需要知道「现在是第几个 chirp、该用长 chirp 还是短 chirp 的参考信号、一帧 Doppler 何时结束」，否则匹配滤波会加载错误的参考、Doppler FFT 会积累错位。承担这件事的模块就是 `radar_mode_controller`。

## 1. 本讲目标

读完本讲，你应当能够：

1. 说清 `radar_mode_controller` 的 **四种工作模式**（STM32 直通 / 自动扫描 / 单 chirp / 保留）各自的使用场景与区别。
2. 画出**自动扫描模式**（`mode == 2'b01`）从 `S_IDLE` 到一帧扫描完成的状态转移图，并解释 `chirp / elevation / azimuth` 三级计数器如何组织成「32 × 31 × 50」的完整扫描。
3. 解释 **Gap 2 运行时配置**：主机用 USB 命令写入的 `cfg_*` 时序参数如何**覆盖**编译期 `parameter`，而又能在不被写入时保持与旧版完全一致（向后兼容）。
4. 说清当主机用 `opcode 0x15` 把 `chirps_per_elev` 改成与 32 不符的值时，为什么会被**钳制到 `DOPPLER_FRAME_CHIRPS`** 并置位 `chirps_mismatch_error`。

## 2. 前置知识

本讲假设你已经具备以下概念（均在前置讲义中建立）：

- **chirp / 脉冲压缩 / staggered PRI**（u5-l1、u4-l2、u4-l4）：发射端用「长 chirp → 监听 → 保护 → 短 chirp → 监听」的节拍交替发射，接收端做匹配滤波把长 chirp 压成窄峰。
- **慢时间 / Doppler 帧**（u4-l4）：把同一个距离门上连续多个 chirp 的回波堆成一列，沿 chirp 方向做 FFT 就得到速度谱。AERIS-10 用「16 长 + 16 短 = 32 chirp」构成一帧 Doppler，对应**双 16 点子帧 FFT**。
- **三级扫描结构**（u5-l1）：`chirp → elevation（仰角）→ azimuth（方位）`，由可编程相移器 ADAR1000 设置递进相位实现电子波束转向，再叠加步进电机的机械扫描。
- **时钟域与 toggle 信号**（u3-l2）：跨时钟域的单拍脉冲要靠「电平翻转 + 多级同步 + 边沿检测」搬运。本模块大量输出 `mc_new_*` 就是这种 **toggle（电平翻转）信号**，供下游做边沿检测还原成脉冲。
- **opcode 与 `host_*` 寄存器**（u3-l1、u6-l2）：主机下发的 4 字节命令 `{opcode, addr, value}` 经命令译码 `case` 表写入一组 `host_*` 配置寄存器，构成 Python `Opcode` 枚举与 Verilog `case` 分支之间的**跨层硬契约**。

> 一个关键认知：`radar_mode_controller` 是**接收域（100 MHz）**的模块，它的输出 `use_long_chirp`、`mc_new_chirp` 等是给**接收链**（`matched_filter_multi_segment`、`chirp_memory_loader_param`、Doppler 处理器）用的，用来告诉接收端「现在该按长 chirp 还是短 chirp 处理」「一帧 Doppler 凑齐了没有」。它**不直接产生射频波形**——那是发射端 `plfm_chirp_controller` 的职责。两者必须保持**节拍同步**，方式有两种：要么由 STM32 同时驱动两边（模式 00），要么各自用相同参数自由运行（模式 01）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [radar_mode_controller.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v) | **本讲主角**。接收端的扫描节拍发生器 | 四种模式、扫描状态机、三级计数器、Gap 2 运行时配置输入 |
| [radar_system_top.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | FPGA 顶层 | `host_*` 时序寄存器声明、默认值、`opcode 0x10–0x15` 译码、`DOPPLER_FRAME_CHIRPS` 钳制 |
| [radar_receiver_final.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v) | 接收机顶层，例化 `rmc` | 把 `host_*` 寄存器接到 `rmc` 的 `cfg_*` 端口、`mode` 接到 `host_mode` |

`radar_mode_controller` 在工程中的位置：

```
radar_system_top.v
  ├── host_long_chirp_cycles ... host_chirps_per_elev   (寄存器，opcode 0x10–0x15 写入)
  ├── DOPPLER_FRAME_CHIRPS = 32                          (固定常量，钳制用)
  └── rx_inst : radar_receiver_final
        └── rmc : radar_mode_controller                  ← 本讲主角
              ├─ mode         ← host_mode (默认 2'b01)
              ├─ cfg_*        ← host_*  (运行时覆盖编译期 parameter)
              └─ 输出 use_long_chirp / mc_new_* / *_count → 匹配滤波 / Doppler / 显示
```

## 4. 核心概念与源码讲解

### 4.1 模式选择：四种工作模式

#### 4.1.1 概念说明

`radar_mode_controller` 不是一个「永远自由运行」的定时器，而是一个**可切换节拍来源**的模块。它有一个 2 位的 `mode` 输入，决定**扫描节拍从哪里来**：

| `mode[1:0]` | 名称 | 节拍来源 | 用途 |
|------------|------|---------|------|
| `2'b00` | STM32 直通 | STM32 通过 GPIO 送来的 toggle 信号 | 正式工作模式：STM32 全权调度，发射/接收严格同步 |
| `2'b01` | 自动扫描 | 模块内部自由运行的定时器 | 默认模式；无需 STM32 也能跑，便于独立测试 FPGA |
| `2'b10` | 单 chirp | 主机 `trigger` 脉冲触发 | 调试：按一下发一个长 chirp，不扫描 |
| `2'b11` | 保留 | — | 空闲，留作将来扩展 |

这四种模式由顶层 `host_radar_mode` 寄存器选择，**默认值是 `2'b01`（自动扫描）**——也就是说，FPGA 上电后即使 STM32 还没接管，接收链也能自己跑起来，这对板级 bring-up（参见 u10-l2）非常友好。

#### 4.1.2 核心流程

模式选择的核心是一段 `case (mode)` 大开关，四个分支互斥，**每个时钟周期都重新评估**当前模式：

```text
每个 posedge clk：
  清除一次性脉冲 (scan_done_pulse <= 0)
  case (mode)
    2'b00 → STM32 直通：检测 stm32_*_toggle 边沿，转发 + 维护计数器
    2'b01 → 自动扫描：跑内部状态机 S_IDLE→S_LONG_CHIRP→...→S_ADVANCE
    2'b10 → 单 chirp：等 trigger_pulse，发一个长 chirp 后回 S_IDLE
    2'b11 → 保留：强制 scan_state <= S_IDLE
  endcase
```

注意：模式切换是**逐周期**生效的，但模式 01 的状态机内部状态 `scan_state` 在模式 00 / 11 里会被强制清回 `S_IDLE`，避免切回自动扫描时从奇怪的状态起步。

#### 4.1.3 源码精读

模块端口里，`mode` 是 2 位输入，注释直接列出了四种编码（[radar_mode_controller.v:54](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L54)）：

```verilog
input wire [1:0] mode,          // 00=STM32, 01=auto, 10=single, 11=rsvd
```

四种模式的 `case` 分支分别位于 [radar_mode_controller.v:177](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L177)（模式 00）、[radar_mode_controller.v:221](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L221)（模式 01）、[radar_mode_controller.v:342](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L342)（模式 10）、[radar_mode_controller.v:379](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L379)（模式 11）。

`mode` 来自顶层 `host_mode`，而 `host_mode` 在接收机里被接到 `host_mode` 信号（[radar_receiver_final.v:151](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L151)），后者由顶层命令译码写入。模式默认值在顶层的复位块里设定为 `2'b01`（[radar_system_top.v:913](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L913)）：

```verilog
host_radar_mode    <= 2'b01;   // Default: auto-scan
```

主机可在运行时用 `opcode 0x01` 改写模式（[radar_system_top.v:951](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L951)）：

```verilog
8'h01: host_radar_mode     <= usb_cmd_value[1:0];
```

模式 10（单 chirp）的触发脉冲则由 `opcode 0x02` 产生（[radar_system_top.v:952](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L952)），它是一个**自清零脉冲**，每个时钟周期末尾被拉低，保证一次命令只发一个 chirp。

#### 4.1.4 代码实践

**实践目标**：验证「模式默认值是自动扫描」并理解模式切换的命令路径。

**操作步骤**（源码阅读型实践，无需硬件）：

1. 打开 [radar_system_top.v:911-914](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L911-L914)，确认 `host_radar_mode` 复位值是 `2'b01`。
2. 打开 [radar_system_top.v:951-952](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L951-L952)，确认 `opcode 0x01` 改模式、`opcode 0x02` 发单 chirp 触发。
3. 沿着 `host_radar_mode` → `rx_inst.host_mode` → `rmc.mode` 追踪，确认模式值确实送到了状态机的 `case(mode)`。

**需要观察的现象 / 预期结果**：

- 复位后 `mode == 2'b01`，`scan_state` 会从 `S_IDLE` 进入 `S_LONG_CHIRP` 开始自由扫描（参见 [radar_mode_controller.v:223-236](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L223-L236)）。
- 若主机发 `opcode 0x01, value=0x02`（模式 10），下一周期 `case` 跳到单 chirp 分支，状态机等 `trigger`。

> 待本地验证：以上行为可用 `tb/` 下的 iverilog 测试平台注入 `opcode` 观察波形确认（参见 u11-l1 的回归测试方法）。

#### 4.1.5 小练习与答案

**练习 1**：为什么默认模式选 `2'b01` 自动扫描，而不是 `2'b00` STM32 直通？

**参考答案**：自动扫描不依赖 STM32 已就绪，FPGA 上电即可独立运行，便于板级 bring-up 和脱离 MCU 的实验室测试。`2'b00` 需要 STM32 真正送来 toggle 信号才有节拍，在 STM32 未启动时接收链会完全静默。

**练习 2**：如果把 `mode` 改成 `2'b11`，模块输出会怎样？

**参考答案**：进入保留分支，`scan_state` 被强制置 `S_IDLE`、`timer` 清零（[radar_mode_controller.v:379-382](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L379-L382)）。由于 `scanning = (scan_state != S_IDLE)`（[radar_mode_controller.v:391](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L391)），`scanning` 输出为 0，表示扫描停止。

---

### 4.2 扫描计数：三级计数器与自动扫描状态机

#### 4.2.1 概念说明

一次完整的雷达扫描（scan）由三级嵌套计数器组织：

\[
\text{一帧 scan} = \text{chirp} \times \text{elevation} \times \text{azimuth} = 32 \times 31 \times 50 = 49\,600 \text{ 个 chirp}
\]

- **chirp（最内层）**：一个仰角方向上连续发射的 chirp 数，固定 32（16 长 + 16 短），正好凑齐一帧 Doppler。
- **elevation（仰角，中层）**：电子波束在垂直方向的 31 个指向。
- **azimuth（方位，最外层）**：水平方向的 50 个指向（部分由电子扫描、部分由步进电机机械扫描）。

这套结构在 `parameter` 里写死（[radar_mode_controller.v:34-36](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L34-L36)）：

```verilog
parameter CHIRPS_PER_ELEVATION = 32,
parameter ELEVATIONS_PER_AZIMUTH = 31,
parameter AZIMUTHS_PER_SCAN = 50,
```

> 注意：`chirp` 这一级在运行时**可被主机改写**（Gap 2，见 4.3），但 `elevation` / `azimuth` 两级仍是编译期参数。

#### 4.2.2 核心流程

模式 01（自动扫描）用一个 7 状态的 FSM 产生「长 chirp → 监听 → 保护 → 短 chirp → 监听 → 推进计数器」的节拍。状态定义见 [radar_mode_controller.v:102-109](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L102-L109)：

```text
S_IDLE         等待启动
S_LONG_CHIRP   长 chirp 持续期（默认 3000 周期 = 30 µs）
S_LONG_LISTEN  长 chirp 后监听（默认 13700 周期 = 137 µs）
S_GUARD        保护间隔（默认 17540 周期 = 175.4 µs，等远距回波衰减）
S_SHORT_CHIRP  短 chirp 持续期（默认 50 周期 = 0.5 µs）
S_SHORT_LISTEN 短 chirp 后监听（默认 17450 周期 = 174.5 µs）
S_ADVANCE      一个 chirp 完成，推进三级计数器
```

状态转移（一帧扫描从 `S_IDLE` 到完成）：

```text
S_IDLE ──(启动)──► S_LONG_CHIRP ──► S_LONG_LISTEN ──► S_GUARD
                       ▲                                     │
                       │                                     ▼
              (chirp++)                              S_SHORT_CHIRP
                │                                        │
                │                                        ▼
                │                                   S_SHORT_LISTEN
                │                                        │
                │                                        ▼
                │◄───────────────────────────────── S_ADVANCE
                │  在 S_ADVANCE 里判断：
                │   chirp < 31 ?        → chirp++，回 S_LONG_CHIRP
                │   elevation < 30 ?    → elevation++，回 S_LONG_CHIRP
                │   azimuth < 49 ?      → azimuth++，回 S_LONG_CHIRP
                │   三级都满            → 全部归零，scan_done_pulse，回 S_LONG_CHIRP（重启）
```

每个 chirp 节拍内的时长由 `timer` 计数器与对应 `cfg_*_cycles` 阈值比较决定（见 4.3）。`S_ADVANCE` 是计数器推进的集中地点，三级 `if / else if / else` 形成嵌套。

> 关于 toggle 输出：每次推进都会翻转 `mc_new_chirp`（以及 `mc_new_elevation` / `mc_new_azimuth`）。这些是**电平翻转信号**而非脉冲——下游模块自己做边沿检测还原成脉冲，这正是 u3-l2 讲的 toggle-CDC 思路在模块内部的应用（虽然这里同在 100 MHz 域，但翻转式接口天然抗亚稳态、便于跨域）。

#### 4.2.3 源码精读

`S_IDLE` 启动第一个 chirp，把三个计数器清零并翻转 `mc_new_chirp`（[radar_mode_controller.v:223-236](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L223-L236)）：

```verilog
S_IDLE: begin
    scan_state     <= S_LONG_CHIRP;
    timer          <= 18'd0;
    use_long_chirp <= 1'b1;
    mc_new_chirp   <= ~mc_new_chirp;  // Toggle to start chirp
    chirp_count    <= 6'd0;
    elevation_count <= 6'd0;
    azimuth_count  <= 6'd0;
end
```

每个计时状态用「`timer < 阈值-1` 则 `timer++`，否则清零并跳下一状态」的固定模式，例如长 chirp（[radar_mode_controller.v:238-246](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L238-L246)）：

```verilog
S_LONG_CHIRP: begin
    use_long_chirp <= 1'b1;
    if (timer < cfg_long_chirp_cycles - 1)
        timer <= timer + 1;
    else begin
        timer <= 18'd0;
        scan_state <= S_LONG_LISTEN;
    end
end
```

三级计数器推进集中在 `S_ADVANCE`（[radar_mode_controller.v:286-332](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L286-L332)），其核心判断结构如下（精简）：

```verilog
S_ADVANCE: begin
    if (chirp_count < cfg_chirps_per_elev - 1) begin
        // 还没凑齐一帧 Doppler 的 chirp 数 → 下一个 chirp
        chirp_count  <= chirp_count + 1;
        mc_new_chirp <= ~mc_new_chirp;
        scan_state   <= S_LONG_CHIRP;
    end else begin
        chirp_count <= 6'd0;
        if (elevation_count < ELEVATIONS_PER_AZIMUTH - 1) begin
            // 当前方位下还有未扫的仰角 → 下一个仰角
            elevation_count  <= elevation_count + 1;
            mc_new_elevation <= ~mc_new_elevation;
            scan_state       <= S_LONG_CHIRP;
        end else begin
            elevation_count <= 6'd0;
            if (azimuth_count < AZIMUTHS_PER_SCAN - 1) begin
                // 还有未扫的方位 → 下一个方位
                azimuth_count  <= azimuth_count + 1;
                mc_new_azimuth <= ~mc_new_azimuth;
                scan_state     <= S_LONG_CHIRP;
            end else begin
                // 三级都满 → 一帧扫描完成，重启
                azimuth_count   <= 6'd0;
                scan_done_pulse <= 1'b1;
                scan_state      <= S_LONG_CHIRP;
            end
        end
    end
end
```

`scan_done_pulse` 经组合逻辑接到输出 `scan_complete`（[radar_mode_controller.v:392](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L392)），是一帧扫描完成的标志脉冲。

> 时长换算（100 MHz，每周期 10 ns）：`LONG_CHIRP_CYCLES=3000` → \(3000 \times 10\,\text{ns} = 30\,\mu\text{s}\)，与发射端 u5-l1 里「3600 样本 ÷ 120 MHz = 30 µs」的长 chirp 时长**完全一致**——接收域用 100 MHz 周期数表达同一个 30 µs，两个域的节拍由此对齐。

#### 4.2.4 代码实践

**实践目标**：手画自动扫描状态转移图，并验证三级计数器的「32 × 31 × 50」结构。

**操作步骤**：

1. 打开 [radar_mode_controller.v:102-109](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L102-L109)，抄下 7 个状态名。
2. 打开 [radar_mode_controller.v:221-336](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L221-L336)，按每个状态的「跳转条件」画箭头（参考 4.2.2 的流程图）。
3. 在 `S_ADVANCE` 里数出三级 `if/else` 嵌套，确认阈值分别是 `cfg_chirps_per_elev-1`、`ELEVATIONS_PER_AZIMUTH-1=30`、`AZIMUTHS_PER_SCAN-1=49`。

**需要观察的现象 / 预期结果**：

- 状态图应是「`S_IDLE` → 一条包含 5 个计时状态的链 → `S_ADVANCE` → 回到 `S_LONG_CHIRP`」的环。
- 在 `S_ADVANCE` 里：`chirp_count` 从 0 数到 31 共 32 次才进位到 elevation；elevation 数 31 次才进位到 azimuth；azimuth 数 50 次才发 `scan_complete`。
- 一帧完整扫描共 \(32 \times 31 \times 50 = 49\,600\) 个 chirp。

> 待本地验证：可写一个最小 iverilog 测试平台，把 `mode` 设 `2'b01`、`cfg_*` 用默认值，跑足够长时间后检查 `azimuth_count` 是否在 0–49 循环、`scan_complete` 是否每 49 600 个 chirp 脉冲一次。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `S_ADVANCE` 里判断用的是 `cfg_chirps_per_elev - 1` 而不是直接 `CHIRPS_PER_ELEVATION - 1`？

**参考答案**：因为 chirp 这一级是 **Gap 2 运行时可配置**的（见 4.3），用 `cfg_chirps_per_elev` 才能让主机写入的新值即时生效；编译期 `CHIRPS_PER_ELEVATION` 只是个不再被状态机使用的「占位默认」。注意主机能写入的值被顶层钳制过（见 4.3.3），不会出现危险的 0 值导致下溢。

**练习 2**：`scan_complete` 一帧扫描会脉冲几次？为什么 `S_ADVANCE` 在三级都满时不是回到 `S_IDLE` 而是回到 `S_LONG_CHIRP`？

**参考答案**：脉冲 1 次（仅 `azimuth` 满且 elevation、chirp 也满的那一拍）。回到 `S_LONG_CHIRP` 而非 `S_IDLE`，是为了**立刻开始下一帧**，让扫描连续不停；只有 `mode` 切走或复位才会回到 `S_IDLE`。

---

### 4.3 运行时配置：Gap 2 如何覆盖编译期参数且向后兼容

#### 4.3.1 概念说明

模块同时有两套时序参数来源：

1. **编译期 `parameter`**：`LONG_CHIRP_CYCLES`、`LONG_LISTEN_CYCLES`、`GUARD_CYCLES`、`SHORT_CHIRP_CYCLES`、`SHORT_LISTEN_CYCLES`、`CHIRPS_PER_ELEVATION`（[radar_mode_controller.v:44-48](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L44-L48)）。改它们要重新综合 bit 流。
2. **运行时 `cfg_*` 输入**：一组从主机 USB 命令来的寄存器（[radar_mode_controller.v:68-73](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L68-L73)）。

这套「双来源」设计在仓库里被称为 **Gap 2**（顶层注释 [radar_system_top.v:233-236](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L233-L236)）。关键设计目标是**向后兼容**：

> 当 `cfg_*` 输入没有被主机写入、保持其默认值（等于编译期 `parameter`）时，模块行为与 Gap 2 引入之前**完全一致**。

实现这一点靠一个简单但巧妙的约定：顶层 `host_*` 寄存器的**复位默认值**与 `radar_mode_controller` 的编译期 `parameter` **逐字相等**，并且 `host_*` 直接接到 `rmc.cfg_*`。于是「没写 = 默认 = 等于 parameter」。

> 为什么叫「Gap 2」？仓库用 Gap 编号标记一系列「补齐缺失能力」的增量改进（Gap 2 = 时序可运行时配置、Gap 4 = USB 读路径，等），便于在注释和测试里引用。这是一种工程上的**变更追踪约定**。

#### 4.3.2 核心流程

数据流：

```text
主机 USB 命令 {opcode 0x10..0x15, value}
        │
        ▼ (命令译码 case 表，带钳制)
顶层 host_long_chirp_cycles / ... / host_chirps_per_elev   (寄存器，默认 = parameter)
        │  (radar_receiver_final 把 host_* 接到 rmc.cfg_*)
        ▼
rmc.cfg_long_chirp_cycles / ... / cfg_chirps_per_elev
        │  (状态机里直接用 cfg_* 而非 parameter)
        ▼
timer 阈值 / chirp 进位阈值
```

`cfg_*` 与 `parameter` 的对应关系（默认值逐字相等）：

| 主机 opcode | 顶层寄存器 | 接到 `rmc.cfg_*` | 编译期 `parameter` | 默认值（二者相等） |
|------------|-----------|------------------|--------------------|--------------------|
| `0x10` | `host_long_chirp_cycles` | `cfg_long_chirp_cycles` | `LONG_CHIRP_CYCLES` | 3000 |
| `0x11` | `host_long_listen_cycles` | `cfg_long_listen_cycles` | `LONG_LISTEN_CYCLES` | 13700 |
| `0x12` | `host_guard_cycles` | `cfg_guard_cycles` | `GUARD_CYCLES` | 17540 |
| `0x13` | `host_short_chirp_cycles` | `cfg_short_chirp_cycles` | `SHORT_CHIRP_CYCLES` | 50 |
| `0x14` | `host_short_listen_cycles` | `cfg_short_listen_cycles` | `SHORT_LISTEN_CYCLES` | 17450 |
| `0x15` | `host_chirps_per_elev` | `cfg_chirps_per_elev` | `CHIRPS_PER_ELEVATION` | 32（受钳制，见 4.3.3） |

#### 4.3.3 源码精读

**（a）模块侧的 `cfg_*` 端口与向后兼容注释**（[radar_mode_controller.v:64-73](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L64-L73)）：

```verilog
// Gap 2: Runtime-configurable timing inputs from host USB commands.
// When connected, these override the compile-time parameters.
// When left at default (tied to parameter values at instantiation),
// behavior is identical to pre-Gap-2.
input wire [15:0] cfg_long_chirp_cycles,
...
input wire [5:0]  cfg_chirps_per_elev,
```

**（b）接收机把 `host_*` 接到 `rmc.cfg_*`**（[radar_receiver_final.v:156-162](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_receiver_final.v#L156-L162)）：

```verilog
// Gap 2: Runtime-configurable timing from host USB commands
.cfg_long_chirp_cycles(host_long_chirp_cycles),
.cfg_long_listen_cycles(host_long_listen_cycles),
.cfg_guard_cycles(host_guard_cycles),
.cfg_short_chirp_cycles(host_short_chirp_cycles),
.cfg_short_listen_cycles(host_short_listen_cycles),
.cfg_chirps_per_elev(host_chirps_per_elev),
```

**（c）顶层 `host_*` 寄存器声明与「默认 = parameter」**（[radar_system_top.v:237-242](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L237-L242)）：

```verilog
reg [15:0] host_long_chirp_cycles;    // Opcode 0x10 (default 3000)
reg [15:0] host_long_listen_cycles;   // Opcode 0x11 (default 13700)
reg [15:0] host_guard_cycles;         // Opcode 0x12 (default 17540)
reg [15:0] host_short_chirp_cycles;   // Opcode 0x13 (default 50)
reg [15:0] host_short_listen_cycles;  // Opcode 0x14 (default 17450)
reg [5:0]  host_chirps_per_elev;      // Opcode 0x15 (default 32)
```

复位块里把这些默认值逐字写入（[radar_system_top.v:918-924](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L918-L924)：

```verilog
// Gap 2: chirp timing defaults (match radar_mode_controller parameters)
host_long_chirp_cycles  <= 16'd3000;
host_long_listen_cycles <= 16'd13700;
host_guard_cycles       <= 16'd17540;
host_short_chirp_cycles <= 16'd50;
host_short_listen_cycles <= 16'd17450;
host_chirps_per_elev    <= 6'd32;
```

**（d）`opcode 0x10–0x15` 译码**（[radar_system_top.v:956-960](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L956-L960)）：`0x10–0x14` 直接把 `usb_cmd_value` 写进对应寄存器；`0x15` 特殊，带钳制。

**（e）`opcode 0x15` 的钳制逻辑**——这是本讲的另一个重点。先看「为什么要钳制」的注释（[radar_system_top.v:245-251](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L245-L251)）：

```verilog
// Fix 4: Doppler/chirps mismatch protection
// DOPPLER_FRAME_CHIRPS is the fixed chirp count expected by the staggered-PRI
// Doppler path (16 long + 16 short). If host sets chirps_per_elev to a
// different value, Doppler accumulation is corrupted. Clamp at command decode
// and flag the mismatch so the host knows.
localparam DOPPLER_FRAME_CHIRPS = 32; // Total chirps per Doppler frame
reg        chirps_mismatch_error;
```

钳制实现（[radar_system_top.v:961-975](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L961-L975)）：

```verilog
8'h15: begin
    // Fix 4: Clamp chirps_per_elev to the fixed Doppler frame size.
    if (usb_cmd_value[5:0] > DOPPLER_FRAME_CHIRPS[5:0]) begin
        host_chirps_per_elev  <= DOPPLER_FRAME_CHIRPS[5:0];   // >32 → 钳到 32
        chirps_mismatch_error <= 1'b1;
    end else if (usb_cmd_value[5:0] == 6'd0) begin
        host_chirps_per_elev  <= DOPPLER_FRAME_CHIRPS[5:0];   // 0 → 钳到 32
        chirps_mismatch_error <= 1'b1;
    end else begin
        host_chirps_per_elev  <= usb_cmd_value[5:0];           // 1..31 → 放行
        // 仅当恰好等于 FFT 帧大小时才清错
        chirps_mismatch_error <= (usb_cmd_value[5:0] != DOPPLER_FRAME_CHIRPS[5:0]);
    end
end
```

**为什么会被钳制到 `DOPPLER_FRAME_CHIRPS`？** 根本原因要回到 u4-l4：Doppler 处理用的是**双 16 点子帧 FFT**，硬件上把一帧的 32 个 chirp 严格分成「16 长 + 16 短」两组分别做 FFT。这个 32 是**硬件固定的架构常量**（`DOPPLER_FRAME_CHIRPS` localparam），不是一个可以随意取值的旋钮。如果允许主机把 `chirps_per_elev` 设成别的值：

- **> 32**：多出来的 chirp 会塞爆 32 深的 Doppler 累加器，导致速度谱错位/混叠 → 必须**向下钳到 32**。
- **== 0**：一个 chirp 都没有，毫无意义，还会让 `cfg_chirps_per_elev - 1` 在状态机里下溢 → 必须**钳到 32**。
- **1..31**：技术上放行（模块仍能跑），但 Doppler FFT 会处理「不完整/不对齐」的帧，速度谱质量下降。这种情况**不强行钳制**，但置位 `chirps_mismatch_error` 让主机意识到数据已降级。

这是一种**防御式设计**：在命令译码的边界就把会破坏下游 DSP 的危险值拦住，而不是让坏值一路传到 Doppler 处理器才暴露成莫名其妙的速度谱错误。`chirps_mismatch_error` 还会回读到状态包里（参见 u6-l2 的状态字段），主机据此决定是否重发命令或提示用户。

#### 4.3.4 代码实践

**实践目标**：动手算一次「`opcode 0x15` 写入不同值」的结果，巩固对钳制逻辑的理解。

**操作步骤**：

1. 打开 [radar_system_top.v:961-975](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L961-L975) 的 `8'h15` 分支。
2. 对下表中每个 `usb_cmd_value[5:0]`，在纸上推演 `host_chirps_per_elev` 和 `chirps_mismatch_error` 的最终值。

| 主机写入 `value[5:0]` | `host_chirps_per_elev` | `chirps_mismatch_error` | 走的分支 |
|----------------------|------------------------|--------------------------|----------|
| `6'd0`（0） | 32 | 1 | 第二分支（0 → 钳到 32） |
| `6'd16`（16） | 16 | 1 | 第三分支（放行，但 ≠32 → 置错） |
| `6'd32`（32） | 32 | 0 | 第三分支（恰好等于 → 清错） |
| `6'd40`（40） | 32 | 1 | 第一分支（>32 → 钳到 32） |

3. 回到 [radar_mode_controller.v:289](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L289)，确认 `S_ADVANCE` 用的是 `cfg_chirps_per_elev - 1`，所以表中 `host_chirps_per_elev` 的值会**立即**改变一帧 Doppler 的 chirp 数。

**需要观察的现象 / 预期结果**：

- 只有写入 `value == 32` 时 `chirps_mismatch_error` 才为 0；其余所有情况都为 1。
- 写入 0 或 >32 时，`host_chirps_per_elev` 最终都是 32，模块仍以 32 chirp / 仰角运行，不会崩。

> 待本地验证：上述表格可用一个针对命令译码的小型 iverilog testbench，注入 `cmd_valid_100m` + 不同 `usb_cmd_value`，观察寄存器与错误标志。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 `chirps_per_elev` 设计成「完全自由可配」，而是要钳制？

**参考答案**：因为 Doppler 处理路径的「16 长 + 16 短 = 32」是**硬件固定的 FFT 帧大小**（双 16 点子帧），不是可变参数。任意非 32 值都会破坏 Doppler 累加的对齐，产生错误的速度谱。钳制 + 错误标志是在命令边界保护下游 DSP 的防御措施。

**练习 2**：如果某天工程师把 `host_long_chirp_cycles` 的复位默认从 `16'd3000` 改成了 `16'd4000`，却忘了同步修改 `radar_mode_controller` 里的 `parameter LONG_CHIRP_CYCLES`，会出问题吗？

**参考答案**：**不会**破坏运行——因为状态机用的是 `cfg_long_chirp_cycles`（即 `host_long_chirp_cycles`），编译期 `LONG_CHIRP_CYCLES` 已经不被状态机引用。但这会让「向后兼容」的注释与 `parameter` 失去对应关系（`parameter` 变成纯粹的文档/占位），属于**可读性退化**。这正是注释里强调「defaults match the parameter values」的原因：两套默认值必须保持逐字相等，否则 `parameter` 就失去了「未被主机写入时的真值」这一语义。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**端到端节拍追踪**任务：

**场景**：FPGA 上电，STM32 尚未接管，主机想用自动扫描模式跑一帧，并临时把保护间隔 `GUARD` 加长到 200 µs（20000 个 100 MHz 周期）来等更远的回波。

**请完成**：

1. **模式**：确认复位后 `mode` 默认是哪个值、走哪个 `case` 分支（参考 4.1）。
2. **时序**：写出主机应发的 `opcode` 与 `value`，把保护间隔改成 20000（参考 4.3.3 的译码表）。指出这条命令最终改的是 `rmc` 的哪个端口、影响状态机的哪个状态（参考 4.2.3 的 `S_GUARD`）。
3. **扫描**：画出从 `S_IDLE` 到第一个 `S_ADVANCE` 之间的状态序列，标出每个状态用的阈值来源（`cfg_guard_cycles` vs `cfg_long_chirp_cycles` 等）。
4. **约束**：若主机改完保护间隔后，又顺手发 `opcode 0x15, value=48` 想把每仰角 chirp 数也加大，结果会怎样？写出 `host_chirps_per_elev` 与 `chirps_mismatch_error` 的值，并解释为什么这个改动**不会**真的让每仰角 chirp 数变成 48（参考 4.3.3 的钳制逻辑与 u4-l4 的 Doppler 帧大小）。

**预期产出**：一份包含「命令字节、状态序列图、钳制结果」的简短报告。如果手头有 iverilog，可写一个只例化 `radar_mode_controller`（把 `cfg_*` 直接绑到 `parameter` 默认值）的 testbench，观察 `chirp_count/elevation_count/azimuth_count` 与 `scan_complete` 的波形来验证第 3 步。否则标注「待本地验证」。

## 6. 本讲小结

- `radar_mode_controller` 是**接收域（100 MHz）**的扫描节拍发生器，输出 `use_long_chirp` 和 `mc_new_*` toggle 信号供匹配滤波 / Doppler / 显示使用，**不直接产生射频波形**。
- 四种 `mode`：`00` STM32 直通（正式工作，节拍由 STM32 GPIO 来）、`01` 自动扫描（**默认**，内部 FSM 自由运行）、`10` 单 chirp（调试）、`11` 保留。模式默认 `2'b01`，可用 `opcode 0x01` 改写。
- 自动扫描用 7 状态 FSM（`S_IDLE → S_LONG_CHIRP → S_LONG_LISTEN → S_GUARD → S_SHORT_CHIRP → S_SHORT_LISTEN → S_ADVANCE`），在 `S_ADVANCE` 集中推进 **chirp × elevation × azimuth = 32 × 31 × 50 = 49 600** 三级计数器。
- **Gap 2** 让 `cfg_*` 运行时输入覆盖编译期 `parameter`，靠「`host_*` 复位默认值 == `parameter`」实现**完全向后兼容**（不写则行为不变）。
- `opcode 0x15`（`chirps_per_elev`）受**钳制保护**：>32 或 ==0 都被钳到 `DOPPLER_FRAME_CHIRPS=32`，1..31 放行但置 `chirps_mismatch_error`。原因是 Doppler 双 16 点子帧 FFT 的 32 是硬件固定常量，不能随意改。

## 7. 下一步学习建议

本讲把**接收端的节拍发生**讲透了。接下来：

- **u6-l1 USB 数据接口**：本讲的 `mc_new_*`、`*_count` 等节拍信号最终配合数据被打包成 11 字节数据包经 USB 上传，可去看 `usb_data_interface_ft2232h.v` / `usb_data_interface.v` 如何把检测点和帧边界（`frame_start`）拼进包里。
- **u6-l2 主机命令协议**：本讲反复出现的 `opcode 0x01/0x10..0x15` 在 Python 侧由 `radar_protocol.py` 的 `Opcode` 枚举构造，去对照 Python↔Verilog 的 case 表一一映射，并看 `chirps_mismatch_error` 如何回读到状态包。
- **u11-l3 跨层契约测试**：`mode` 编码、`opcode` 与 `chirps_per_elev` 钳制行为是典型的「三层契约」，去看 `test_cross_layer_contract.py` 如何用独立真值同时校验 Python、Verilog、C 三层一致性。
- **u14-l1 形式化验证**：本模块的状态机是 `formal/fv_radar_mode_controller.sby` 的验证对象之一，学完本讲后可去读 `fv_*.v` 看它如何**证明**「状态机不会卡死、计数器不会越界」这类仿真难以穷尽的性质。
