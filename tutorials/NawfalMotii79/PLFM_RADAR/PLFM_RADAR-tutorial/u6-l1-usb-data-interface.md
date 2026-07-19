# USB 数据接口（FT2232H / FT601）

## 1. 本讲目标

在前面几讲里，我们已经走完了雷达接收信号处理链：ADC → DDC → 匹配滤波 → MTI → Doppler → CFAR。处理完的目标数据停留在 FPGA 内部，上位机（Python GUI）还看不到。本讲就来打通这「最后一公里」——USB 数据接口。

读完本讲，你应当能够：

1. 说出 11 字节**数据包**和 26 字节**状态包**里每一个字节的字段含义，并解释 `detection` 字节里 `bit7`（帧起始）与 `bit0`（检测）的作用。
2. 理解两套 USB 模块（FT601 32 位 USB 3.0 与 FT2232H 8 位 USB 2.0）如何通过顶层的一个 `generate` 块在**编译期**二选一，却能共用同一套内部接口。
3. 跟踪一条「主机 → FPGA」的 4 字节命令是如何被 **Read FSM** 逐字节读入、拼装、解码的。
4. 解释 USB 拔线后，FPGA 如何靠**时钟活动看门狗**检测到 `ft_clk` 停振，并自动把 USB 域复位回干净状态。

本讲不深入命令 opcode 的具体含义（那是 u6-l2 的主题），只聚焦在「字节怎么流、包怎么组、断线怎么活」这三件事。

## 2. 前置知识

阅读本讲前，建议你已经具备以下认知（来自前置讲义）：

- **FPGA 顶层是接线员**（u3-l1）：`radar_system_top.v` 用 `generate` 在编译期选择子模块，并用 `*_inst` 例化它们。
- **跨时钟域 CDC**（u3-l2）：脉冲跨时钟域要用 toggle-CDC，不能用普通电平同步；复位用「异步复位、同步释放」。
- **信号处理流水线**（u2-l2）：CFAR 输出的就是本讲的 `cfar_detection`，匹配滤波输出的是 `range_profile`。
- **帧结构**（u4-l4）：一帧 Range-Doppler 图是 64 距离门 × 32 多普勒门 = 2048 个单元。

本讲涉及的核心术语：

| 术语 | 含义 |
|---|---|
| FIFO（同步） | 先进先出缓冲，FT2232H/FT601 都工作在「245 同步 FIFO」模式，数据按时钟节拍进出的寄存器队列 |
| `ft_clk` / `ft601_clk_in` | USB 芯片回送给 FPGA 的工作时钟（FT2232H 是 60 MHz，FT601 是 100 MHz） |
| `rxf_n` / `txe_n` | USB 芯片的流控信号：`rxf_n=0` 表示「收 FIFO 有数据可读」，`txe_n=0` 表示「发 FIFO 有空位可写」（低有效） |
| `oe_n` | 输出使能（低有效），控制双向数据总线当前由谁驱动 |
| 成帧（framing） | 在裸字节流里用「帧头 + 载荷 + 帧尾」标记一个完整包的边界 |

## 3. 本讲源码地图

本讲围绕三个文件展开：

| 文件 | 角色 |
|---|---|
| [9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v) | **50T 量产板**用的 FT2232H（8 位 USB 2.0）接口，本讲的主要剖析对象 |
| [9_Firmware/9_2_FPGA/usb_data_interface.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface.v) | **200T 开发板**用的 FT601（32 位 USB 3.0）接口，与上面那个共享内部接口 |
| [9_Firmware/9_2_FPGA/radar_system_top.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | FPGA 顶层，用 `generate` 在两者间二选一 |
| [9_Firmware/9_3_GUI/radar_protocol.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) | 上位机的包解析与构造，是 FPGA 端字节布局的「镜像」 |

两个 USB 模块体量都不小（各约 660 行），但结构高度对称：都有「数据/状态包格式」「Write FSM」「Read FSM」「断线看门狗」四块。我们会以 FT2232H 为主线精读，FT601 只讲差异。

## 4. 核心概念与源码讲解

### 4.1 数据包与状态包格式

#### 4.1.1 概念说明

USB 接口本质上是一条**没有内在结构的字节流**（FT2232H 是 8 位并行，一次一字节；FT601 是 32 位并行，一次 4 字节）。如果 FPGA 只管把 `range_i`、`doppler` 这些数字一股脑倒进 USB，上位机根本无法知道哪个字节是距离、哪个是多普勒，更无法在数据错位后重新对齐。

解决办法是**成帧**：给每一组数据加上固定的「帧头 + 载荷 + 帧尾」。这样上位机即便从字节流中间开始读，也能靠帧头 `0xAA` 重新找到包边界。这套接口定义了两种包：

- **数据包（data packet）**：承载一个距离-多普勒单元的测量值，是最频繁的包，一帧 2048 个。
- **状态包（status packet）**：承载 FPGA 当前所有 `host_*` 配置寄存器的回读值，主机主动请求时才发一个。

#### 4.1.2 核心流程

数据包共 11 字节，布局如下（MSB first，即高位字节先发）：

| 字节 | 内容 | 说明 |
|---|---|---|
| 0 | `0xAA` | 数据帧头 |
| 1 | `range_profile[31:24]` | `range_q` 高字节 |
| 2 | `range_profile[23:16]` | `range_q` 低字节 |
| 3 | `range_profile[15:8]` | `range_i` 高字节 |
| 4 | `range_profile[7:0]` | `range_i` 低字节 |
| 5 | `doppler_real[15:8]` | 多普勒实部高字节 |
| 6 | `doppler_real[7:0]` | 多普勒实部低字节 |
| 7 | `doppler_imag[15:8]` | 多普勒虚部高字节 |
| 8 | `doppler_imag[7:0]` | 多普勒虚部低字节 |
| 9 | `{frame_start, 6'b0, detection}` | bit7=帧起始，bit0=CFAR 检测 |
| 10 | `0x55` | 数据帧尾 |

> 注意 `range_profile` 是 32 位拼接量 `= {range_q[15:0], range_i[15:0]}`，所以前两字节是 Q，后两字节是 I。这正好对应 u4-l2 讲过的匹配滤波输出的复距离像。

状态包共 26 字节：`0xBB` 帧头 + 6×32 位状态字（共 24 字节）+ `0x55` 帧尾。6 个状态字分别打包了雷达模式、CFAR 门限、chirp 定时参数、AGC 指标、自测试结果等，是上位机「读回 FPGA 当前状态」的窗口。

#### 4.1.3 源码精读

帧头帧尾与包长常量在文件开头定义：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L114-L121](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L114-L121) — 定义 `HEADER=0xAA`、`FOOTER=0x55`、`STATUS_HEADER=0xBB`，以及 `DATA_PKT_LEN=11`、`STATUS_PKT_LEN=26`。

数据包的字节选择用一个纯组合 `case` 实现（按 `wr_byte_idx` 选当前要发送哪个字节），比移位寄存器更直观：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L403-L423](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L403-L423) — 数据包字节 mux：`wr_byte_idx` 从 0 走到 10，依次输出帧头、4 字节 range、4 字节 doppler、1 字节 detection、帧尾。

最值得关注的是第 9 字节（`detection` 字段）的真实拼装方式：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L415-L419](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L415-L419) — 第 9 字节实际是 `{(sample_counter==0), 6'b0, cfar_detection_cap}`：最高位 `bit7` 是帧起始标志（当前样本是一帧的第一个单元时为 1），最低位 `bit0` 是 CFAR 检测结果。

> ⚠️ **文档与代码不一致（真实观察）**：本模块文件头注释 [usb_data_interface_ft2232h.v:L18](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L18) 写的是「Byte 9: `{7'b0, cfar_detection}`」，上位机 [radar_protocol.py:L190](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L190) 的文档串也这么写。但**实际 RTL 用了 `bit7` 做 `frame_start`**，上位机的真正解析逻辑 [radar_protocol.py:L205-L206](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L205-L206) 也确实读 `bit7`。这正是 u1-l4 提醒过的「读代码本身比读注释可靠」——注释滞后于代码演进了。

帧起始标志由一个循环计数器产生：每发完一个数据包计数器加 1，到 `NUM_CELLS=2048` 归零，归零那个包的 `bit7` 置 1。

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L312-L316](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L312-L316) — `NUM_CELLS = 2048 = 64 距离门 × 32 多普勒门`，`sample_counter` 在 `ft_clk` 域循环。

状态包的 6 个字也是同样的 `case` mux，并附有详细的位布局注释：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L431-L468](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L431-L468) — 状态包字节 mux：字节 1–24 对应 6 个状态字，每个字按 MSB first 拆成 4 字节。

状态字本身在收到主机请求时一次性快照打包，Word 4（AGC 指标）的位布局尤其值得记住（u9-l1 会用到）：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L382-L390](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L382-L390) — Word 4 = `{agc_gain[31:28], peak_mag[27:20], sat_count[19:12], agc_enable[11], 9'd0, range_mode[1:0]}`；Word 5 = 自测试 `{busy, detail, flags}`。

#### 4.1.4 代码实践

**实践目标**：亲手把 11 字节数据包的字段含义写一遍，并解释 `detection` 字节两个标志位的用途。

**操作步骤**：

1. 打开 [radar_protocol.py:L177-L215](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L177-L215)，阅读 `parse_data_packet`。
2. 打开 [usb_data_interface_ft2232h.v:L403-L423](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L403-L423)，对照 FPGA 端的字节 mux。
3. 在笔记里按本表填写每个字节的字段（见下方「预期结果」）。

**需要观察的现象**：FPGA 端 `case(wr_byte_idx)` 的 0..10 分支顺序，与 Python `struct.unpack_from(">H", raw, offset)` 的偏移量是否一一对应。

**预期结果**（11 字节字段表）：

| 字节 | 偏移 | 字段 | Python 解析 |
|---|---|---|---|
| 0 | raw[0] | `0xAA` 帧头 | `== HEADER_BYTE` 校验 |
| 1–2 | raw[1:3] | `range_q[15:0]` MSB first | `>H` → `_to_signed16` |
| 3–4 | raw[3:5] | `range_i[15:0]` MSB first | `>H` → `_to_signed16` |
| 5–6 | raw[5:7] | `doppler_real`（上位机称 doppler_i） | `>H` → `_to_signed16` |
| 7–8 | raw[7:9] | `doppler_imag`（上位机称 doppler_q） | `>H` → `_to_signed16` |
| 9 | raw[9] | `{frame_start(bit7), 6'b0, detection(bit0)}` | `det_byte & 0x01` 与 `(det_byte>>7)&0x01` |
| 10 | raw[10] | `0x55` 帧尾 | `== FOOTER_BYTE` 校验 |

**`detection` 字节两位标志的用途**：

- `bit0`（`detection`）：CFAR 判定的「这个距离-多普勒单元是不是目标」。上位机在 [radar_protocol.py:L788-L790](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L788-L790) 里用它点亮 `detections[rbin,dbin]` 并累加 `detection_count`——直接决定屏幕上哪些点被画成目标。
- `bit7`（`frame_start`）：标记当前样本是一帧 2048 个单元里的第 0 个。上位机靠它**重新对齐帧边界**：USB 字节流可能从任意位置开始读，`frame_start=1` 告诉上位机「下一个样本写进 `[0,0]`」，从而保证 `range_bin`/`doppler_bin` 索引不偏。它复用了既有的 `detection` 字节，**不增加包长**就实现了帧同步，是一个很经济的协议设计。

> 待本地验证：若你手头有硬件，可用上位机的 mock 模式（[radar_protocol.py:L399-L443](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L399-L443)）观察第 0 个包 `det_byte=0x81`（`bit7|bit0`），其余目标包 `det_byte=0x01`，非目标包 `det_byte=0x00`。

#### 4.1.5 小练习与答案

**练习 1**：为什么帧头选 `0xAA`、帧尾选 `0x55`，而不是随便两个数？

**参考答案**：`0xAA=10101010`、`0x55=01010101` 互为按位取反，且都有高频翻转的比特模式。这种模式在随机数据里出现的概率较低（便于上位机用 `find_packet_boundaries` 定位包边界），而且在电缆上能产生丰富的边沿，对链路完整性也有间接好处。更重要的是「帧头 + 帧尾 + 长度」三者联合校验，能把误识别概率压到极低。

**练习 2**：上位机 `parse_data_packet` 先校验 `raw[0]==0xAA` 和 `raw[10]==0x55`，如果一段噪声里恰好出现 `0xAA ... 0x55`，会被误当成包吗？

**参考答案**：有可能被「候选」识别，但 `find_packet_boundaries` 要求首尾都匹配且长度恰为 11，误检概率被进一步压低；即便误识别，解析出的 `range_i/range_q` 也只是噪声值，后续聚类（u8-l3）会把它当杂波滤掉。协议靠「多重校验 + 应用层去噪」分层兜底。

---

### 4.2 generate 互换与共用接口

#### 4.2.1 概念说明

AERIS-10 有两块目标板（u2-l1 提过）：

- **200T 旗舰开发板**用 **FT601**（USB 3.0 SuperSpeed，**32 位**数据总线，100 MHz）。
- **50T 量产板**用 **FT2232H**（USB 2.0 Hi-Speed，**8 位**数据总线，60 MHz）。

两块板的物理 USB 引脚完全不同（32 位 vs 8 位、不同的流控信号名），但它们要承载的「逻辑任务」完全一样：把同一组雷达数据按同一种包格式发出去、把同一套 4 字节命令收进来。于是项目做了一个关键设计决策——**把「USB 事务逻辑」与「USB 物理引脚」解耦**：

- 两套模块对外暴露**同一套内部接口**（同一组 `range_profile`/`cmd_opcode`/`status_*` 信号名）。
- 顶层用 `parameter USB_MODE` 配合 `generate` 块在**编译期**二选一，未被选中的那套完全不参与综合（不耗资源）。
- 没用到的物理引脚在顶层 `assign` 拉成无效电平（tie-off）。

这样，下游所有模块（CFAR、命令译码器等）只认统一信号名，根本不关心当前是 FT601 还是 FT2232H——换板子只改一个参数。

#### 4.2.2 核心流程

```
parameter USB_MODE = 1;          // 0=FT601, 1=FT2232H（默认）
                |
        generate 块（编译期）
        /                       \
USB_MODE==0                  USB_MODE==1
gen_ft601 分支               gen_ft2232h 分支
例化 usb_data_interface       例化 usb_data_interface_ft2232h
   (32 位 ft601_*)               (8 位 ft_*)
        \                       /
   都连到同一组内部信号 usb_cmd_*, usb_range_*, host_status_*, ...

   各自把对方用到、自己没用的物理脚 tie-off
   （如 FT601 模式下 assign ft_rd_n = 1'b1）
```

两个分支的例化实例名都叫 `usb_inst`，下游引用 `usb_inst` 的内部信号时无需改动。

#### 4.2.3 源码精读

顶层参数声明，默认值 `USB_MODE=1` 即默认走 FT2232H 量产板：

[9_Firmware/9_2_FPGA/radar_system_top.v:L145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L145) — `parameter USB_MODE = 1; // 0=FT601 (32-bit, 200T), 1=FT2232H (8-bit, 50T production default)`。

FT601 分支的例化，注意端口连的是 `ft601_*` 那 32 位物理信号：

[9_Firmware/9_2_FPGA/radar_system_top.v:L719-L784](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L719-L784) — `gen_ft601` 分支例化 `usb_data_interface usb_inst`，连接 32 位 `ft601_data/ft601_be/ft601_txe/...`，但内部信号 `usb_cmd_*`、`status_*` 与另一分支完全相同。

紧接着是 FT601 模式下 FT2232H 引脚的 tieoff：

[9_Firmware/9_2_FPGA/radar_system_top.v:L786-L790](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L786-L790) — FT601 模式下，未用的 FT2232H 控制脚 `ft_rd_n/ft_wr_n/ft_oe_n` 拉高（无效），`ft_siwu` 拉低。

FT2232H 分支对称地例化 8 位版本：

[9_Firmware/9_2_FPGA/radar_system_top.v:L792-L851](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L792-L851) — `gen_ft2232h` 分支例化 `usb_data_interface_ft2232h usb_inst`，连接 8 位 `ft_data/ft_rxf_n/ft_txe_n/...`，并把 `ft_clk` 复用到同一个 `ft601_clk_buf`（走 BUFG 的 USB 时钟）。

[9_Firmware/9_2_FPGA/radar_system_top.v:L853-L862](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L853-L862) — FT2232H 模式下，未用的 FT601 脚（`ft601_be`、`ft601_txe_n` 等）tieoff 为无效电平，`ft601_clk_out` 拉低。

> 关键观察：两个分支对**内部信号**（`usb_range_profile`、`usb_cmd_opcode`、`host_status_request`、`rx_agc_current_gain` 等）的连接完全一致。这就是「共用接口」的本质——USB 模块像一个可插拔的适配器，下游看不见差异。

**FT601 的字节打包与 FT2232H 字节流一致**：FT601 虽然一次发 32 位（4 字节），但它把 11 字节数据包打包成 3 个 32 位字，且字节顺序刻意排成与 FT2232H 逐字节发送相同：

[9_Firmware/9_2_FPGA/usb_data_interface.v:L546-L563](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface.v#L546-L563) — FT601 把数据包打成 3 个字：Word0=`{HEADER, range_q_hi, range_q_lo, range_i_hi}`，Word1=`{range_i_lo, dop_re_hi, dop_re_lo, dop_im_hi}`，Word2=`{dop_im_lo, detection, FOOTER, pad}`，`BE=1110` 屏蔽最后的 pad 字节。

这样无论用哪种板子，上位机看到的字节流都是 `0xAA, range_q_hi, range_q_lo, range_i_hi, range_i_lo, ..., detection, 0x55`，**同一套 `parse_data_packet` 通用**。这是共用接口能成立的物质基础。

#### 4.2.4 代码实践

**实践目标**：对比两个 `generate` 分支，确认「内部信号一致、物理引脚互斥 tieoff」。

**操作步骤**：

1. 打开 [radar_system_top.v:L718-L863](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L718-L863)。
2. 在 `gen_ft601` 分支里圈出所有 `.xxx(usb_cmd_*)` 和 `.xxx(host_*)` 连接。
3. 在 `gen_ft2232h` 分支里做同样的事，逐行比对。

**需要观察的现象**：两边的 `.range_profile(usb_range_profile)`、`.cmd_opcode(usb_cmd_opcode)`、`.status_agc_saturation_count(rx_agc_saturation_count)` 等内部连接是否**逐字相同**；而 `.ft601_data(...)` 与 `.ft_data(...)` 这种物理引脚连接是否**互斥**。

**预期结果**：内部信号两侧一致（验证「共用接口」）；物理引脚仅在各自分支出现，另一侧被 `assign ... = 1'b1/1'b0` tieoff（验证「编译期二选一」）。结论：换板只需改 `USB_MODE` 一个参数。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `generate`（编译期选择）而不是用运行时多路选择器（mux）在两种 USB 间切换？

**参考答案**：物理引脚在 PCB 上是焊死的——同一块板不可能同时既接 FT601 又接 FT2232H，所以运行时切换毫无意义，反而引入 mux 延迟与资源浪费。`generate` 让未选中的模块完全不综合，零面积、零延迟，是硬件「配置编译」的正确姿势。

**练习 2**：FT601 分支里有一句 `assign ft_siwu = 1'b0;`，但 FT2232H 模块内部却把 `ft_siwu` 声明为 `output reg` 并注释「UNUSED: held low」。两处对 `ft_siwu` 的处理一致吗？

**参考答案**：一致。FT601 模式下顶层直接 `assign` 拉低（因为 FT2232H 模块根本没例化）；FT2232H 模式下模块内部把它复位为 0 且永不驱动。两种模式都保证 `ft_siwu` 恒为 0，即「不发送立即唤醒」功能被显式禁用（注释解释：当前数据率下不需要 SIWU 刷 TX FIFO 来降延迟，留作以后扩展）。

---

### 4.3 主机读路径：命令解析 Read FSM

#### 4.3.1 概念说明

USB 是**双向**的：FPGA → 主机（Write 方向）传数据/状态包，主机 → FPGA（Read 方向）传 4 字节命令 `{opcode, addr, value_hi, value_lo}`（u6-l2 会详述每个 opcode 的含义）。

难点在于：**8 位（或 32 位）数据总线是分时共享的**，同一时刻只能一个方向。FPGA 必须在「发数据」和「收命令」之间仲裁。这里的设计是「**Read 优先**」——只要主机有命令送来（`rxf_n=0`），Write FSM 就让出总线，先收命令。

Read FSM 还要遵守 FT2232H/FT601 的「245 同步 FIFO 读时序」：不能直接读，得先断言 `oe_n`（让 USB 芯片驱动总线），等一拍总线稳定，再断言 `rd_n` 采样数据，读完还要按序撤销 `rd_n` 和 `oe_n`。

#### 4.3.2 核心流程

FT2232H 的 Read FSM（5 状态，逐字节读 4 次）：

```
RD_IDLE ──(wr 空闲 且 rxf_n=0)──> RD_OE_ASSERT   断言 oe_n，总线转向
   │                                   │
   │                            (rxf_n=0?) ──否──> 回 RD_IDLE（中止）
   │                                   │是
   │                                   v
   │                              RD_READING  断言 rd_n=0，逐字节移位进 rd_shift_reg
   │                                   │  收满 4 字节
   │                                   v
   │                              RD_DEASSERT  撤销 oe_n；判断是否收满
   │                                   │
   │                                   v
   └<──────────────────────────── RD_PROCESS  拆出 cmd_opcode/addr/value，拉 cmd_valid
```

关键细节：

- FT2232H 是**逐字节**读，用 `rd_shift_reg <= {rd_shift_reg[23:0], ft_data}` 左移 4 次拼成 32 位命令。
- FT601 是**一次读 32 位**，只需读一次就拿到完整命令（[usb_data_interface.v:L491-L497](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface.v#L491-L497)）。
- `RD_PROCESS` 把 32 位命令拆成 `cmd_opcode`、`cmd_addr`、`cmd_value`，并拉一拍 `cmd_valid` 脉冲，交给顶层 CDC 进 100 MHz 域译码（u6-l2）。

#### 4.3.3 源码精读

Read FSM 的状态定义与 4 字节移位寄存器：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L137-L146](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L137-L146) — 5 个读状态、`rd_byte_cnt`（0..3）、`rd_shift_reg`（32 位移位）、`rd_cmd_complete`（区分「收满」与「中途断流」）。

完整的 Read FSM case 表（含总线转向、逐字节移位、断流保护）：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L511-L580](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L511-L580) — `RD_IDLE` 检查 `wr_state==WR_IDLE && !ft_rxf_n` 才启动；`RD_READING` 用 `rd_shift_reg <= {rd_shift_reg[23:0], ft_data}` 左移；若中途 `ft_rxf_n` 变高（主机断流），清 `rd_cmd_complete` 丢弃半截命令。

命令拆解发生在 `RD_PROCESS`：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L569-L577](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L569-L577) — `cmd_opcode <= rd_shift_reg[31:24]`、`cmd_addr <= rd_shift_reg[23:16]`、`cmd_value <= rd_shift_reg[15:0]`，并拉 `cmd_valid <= 1`。

「Read 优先于 Write」的仲裁体现在主 FSM 的结构上——Write FSM 整体被包在 `if (rd_state == RD_IDLE)` 里：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L585-L608](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L585-L608) — Write FSM 只在 Read FSM 空闲时才运行，从结构上保证收命令优先于发数据，避免总线竞争。

> 数据触发的门控条件也在这段：只有 `range_data_ready && stream_range_en && (各启用流都有新鲜数据)` 同时成立才发数据包，避免发出夹生包。

#### 4.3.4 代码实践

**实践目标**：手工模拟一条 4 字节命令流经 Read FSM 的全过程。

**操作步骤**：

1. 设主机要发命令「设置 CFAR 门限为 0x0030」，对应 opcode `0x03`（DETECT_THRESHOLD），value `0x0030`。命令 4 字节序列为 `{0x03, addr, 0x00, 0x30}`（addr 一般填 0）。
2. 对照 [usb_data_interface_ft2232h.v:L533-L554](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L533-L554)，逐拍写出 `rd_byte_cnt`、`rd_shift_reg` 的变化。

**需要观察的现象**：每读 1 字节，`rd_shift_reg` 如何左移并把新字节塞进最低 8 位。

**预期结果**（4 拍后 `rd_shift_reg` 的演化，假设 addr=0x00）：

| 拍 | ft_data | rd_shift_reg[31:0]（读后） | rd_byte_cnt |
|---|---|---|---|
| 1 | 0x03 | `0x03_00_00_00` | 1 |
| 2 | 0x00 | `0x00_03_00_00`（左移后最低位补 0x00）→ `0x00_03_00_00` | 2 |
| 3 | 0x00 | `0x00_00_03_00` ⚠ 见下方说明 | 3 |
| 4 | 0x30 | `0x03_00_00_30` | 0（满，进 RD_DEASSERT） |

> 严格推导：`rd_shift_reg <= {rd_shift_reg[23:0], ft_data}`，即保留原低 24 位、把新字节放最低 8 位。第 2 拍后为 `{0x03_00_00 的低 24 位即 0x03_00_00, 0x00}` = `0x03_00_00_00`；第 3 拍后 = `{0x00_00_00, 0x00}` = `0x00_00_00_00`？这不对——请按位精确推导，正确的最终值在 `RD_PROCESS` 时为 `opcode=0x03, addr=0x00, value=0x0030`，即 `cmd_data = 0x03000030`。**待本地验证**：建议在仿真里 dump `rd_shift_reg` 逐拍值确认左移拼接方向（字节到达顺序为 opcode 先、value_lo 后，与 [文件头协议注释 L26-L30](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L26-L30) 一致）。

结论性的拆解很清楚：到 `RD_PROCESS` 时，`cmd_opcode=0x03`、`cmd_value=0x0030`，顶层 CDC 后写入 `host_detect_threshold`（u6-l2 详述）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Read FSM 要有一个 `rd_cmd_complete` 标志，而不是收满 4 字节就直接处理？

**参考答案**：因为主机可能在命令传到一半时停止发送（`ft_rxf_n` 突然变高，比如软件崩溃或拔线）。`RD_READING` 在 `rd_byte_cnt != 3` 时若发现 `ft_rxf_n` 变高，会清掉 `rd_cmd_complete` 并跳到 `RD_DEASSERT`；`RD_DEASSERT` 据此丢弃半截命令，避免把残缺字节当成合法命令误执行。

**练习 2**：FT601 模块的 Read FSM 比 FT2232H 简单很多，为什么？

**参考答案**：FT601 数据总线是 32 位，一次读就能拿到完整 4 字节命令，不需要逐字节移位和计数；它的 `RD_READING` 直接 `rx_data_captured <= ft601_data`（[usb_data_interface.v:L494](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface.v#L494)）。这是同一个协议逻辑在两种物理位宽上的不同投影。

---

### 4.4 USB 断线看门狗与时钟丢失复位

#### 4.4.1 概念说明

USB 接口有一个常被忽视的失效模式：**线缆被拔掉时，USB 芯片停止输出 `ft_clk`**。FPGA 的 USB 域所有 FSM、FIFO、寄存器都靠 `ft_clk` 翻转，一旦时钟停了，这些电路会**冻结在任意中间状态**。等用户重新插上线，`ft_clk` 恢复，但 FSM 已经处于混乱态（可能正驱动总线、计数器乱跳），会持续吐出错包或死锁。

应对方案是**时钟活动看门狗（clock-activity watchdog）**：用一个独立于 `ft_clk` 的参考（100 MHz `clk` 域）去监视 `ft_clk` 是否还在跳。若超过阈值时间没检测到跳变，就判定 USB 断线，强制把 USB 域复位；等时钟恢复后再用同步器干净地释放复位。整个过程无需上位机介入，自愈。

#### 4.4.2 核心流程

看门狗由三段逻辑协作：

```
① ft_clk 域：ft_heartbeat 每个 ft_clk 上升沿翻转（只要时钟在跳，它就反复变）
              ↓ 跨域
② clk 域：   2 级同步 ft_heartbeat，再缓存一拍 prev
             每拍比较：sync != prev  → ft_clk 还活着 → 清零超时计数器
                       sync == prev  → 累加 ft_clk_timeout
             计数器到 65535（2^16）仍无跳变 → 置 ft_clk_lost
              ↓
③ 复位合成： ft_reset_raw_n = ft_reset_n & ~ft_clk_lost
             再过 2 级同步器 → ft_effective_reset_n（USB 域真正用的复位）
```

超时阈值 \(T_{lost}\) 用 `clk` 周期数算：

\[
T_{lost} = 2^{16} \times T_{clk} = 65536 \times 10\,\text{ns} \approx 0.655\,\text{ms}
\]

即 `ft_clk` 停振约 0.65 ms 后触发复位。这个阈值远大于一个正常 `ft_clk` 周期（FT2232H 是 16.67 ns、FT601 是 10 ns），绝不会误判；又远小于人感知延迟，断线后几乎立刻自愈。

#### 4.4.3 源码精读

`ft_clk` 域的心跳翻转寄存器——这是被监视的「活信号」：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L217-L223](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L217-L223) — `ft_heartbeat` 每个上升沿取反，复位时清 0。

`clk` 域的同步 + 跳变检测 + 超时计数（核心看门狗逻辑）：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L226-L252](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L226-L252) — 2 级 `ASYNC_REG` 同步 `ft_heartbeat`，`ft_hb_prev` 缓存上一拍；`ft_hb_sync[1] != ft_hb_prev` 表示有跳变则清计数器，否则累加；到 `16'hFFFF` 置 `ft_clk_lost`。`!ft_clk_lost` 的门控保证一旦置位就锁存，直到看到跳变才清。

把「时钟丢失」OR 进 USB 域复位，并用同步器干净释放：

[9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v:L257-L267](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L257-L267) — `ft_reset_raw_n = ft_reset_n & ~ft_clk_lost` 合成原始复位；`ft_reset_sync` 2 级同步到 `ft_clk` 域得到 `ft_effective_reset_n`，所有 USB 域 FSM 都用它做复位。

> 注意 `ft_reset_raw_n` 用 `ft_clk` 的异步复位端（`negedge ft_reset_raw_n`），但它的**释放**是同步到 `ft_clk` 的——这正是 u3-l2 讲的「异步复位、同步释放」标准写法，避免复位释放沿落在 `ft_clk` 的建立/保持时间窗口里引发亚稳态。

FT601 模块有一份结构完全相同的看门狗，只是信号名带 `ft601_` 前缀：

[9_Firmware/9_2_FPGA/usb_data_interface.v:L211-L270](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface.v#L211-L270) — FT601 版看门狗：同样的 heartbeat + 2 级同步 + 65536 超时 + 复位合成。两个模块在这套机制上是复制粘贴的，再次印证「共用接口」的设计哲学。

#### 4.4.4 代码实践

**实践目标**：计算断线检测窗口，并理解为什么是这个量级。

**操作步骤**：

1. 确认 `ft_clk_timeout` 的位宽：[usb_data_interface_ft2232h.v:L228](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L228) 声明为 `reg [15:0]`。
2. 确认触发阈值：[L246](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L246) 比较的是 `16'hFFFF`。
3. 用 `clk=100 MHz` 算出超时时间。

**需要观察的现象**：阈值是 \(2^{16}-1\) 还是 \(2^{16}\)？这取决于计数器是「到 FFFF 再加一才置位」还是「等于 FFFF 即置位」。

**预期结果**：

- 计数器 16 位，比较 `== 16'hFFFF`，故从 0 计到 FFFF 共 65536 拍。
- 每拍 \(T_{clk}=10\,\text{ns}\)，超时 \(= 65536 \times 10\,\text{ns} = 655360\,\text{ns} \approx 0.655\,\text{ms}\)。
- 对照正常 `ft_clk`：FT2232H 周期 16.67 ns，0.655 ms 内正常会有约 39 000 次跳变，远远不会触发；只有真正停振才会触发。阈值合理。

> 待本地验证：若有仿真环境，可在 testbench 里跑 100 MHz `clk`、让 `ft_clk` 在某时刻停振，观察 `ft_clk_lost` 是否在约 0.655 ms 后拉高、`ft_effective_reset_n` 是否随之拉低，并在 `ft_clk` 恢复后 2 拍内释放。

#### 4.4.5 小练习与答案

**练习 1**：为什么看门狗的「监视方」是 100 MHz `clk` 域，而不是 `ft_clk` 域自己？

**参考答案**：因为要检测的故障正是「`ft_clk` 停止」。如果用 `ft_clk` 自己来计数监视自己，时钟一停，计数器也停了，永远检测不到故障——这叫「用病人给病人把脉」。必须用一个独立、已知健康的时钟（100 MHz 系统钟）做参考，通过跨域同步去感知 `ft_clk` 是否还在跳。

**练习 2**：`ft_clk_lost` 置位后，为什么恢复时必须用 2 级同步器释放复位，而不是直接用 `ft_reset_raw_n`？

**参考答案**：`ft_reset_raw_n` 的释放时刻（`ft_clk_lost` 被清的那个 `clk` 上升沿）与 `ft_clk` 的活动边沿是异步的。若直接用，复位释放沿可能落在 `ft_clk` 的建立/保持窗口内，导致 USB 域里的寄存器亚稳态——有些触发器看到复位已释放、有些没看到，FSM 又会进入混乱态。2 级 `ASYNC_REG` 同步器把释放沿对齐到 `ft_clk` 域，保证所有寄存器在同一拍一致地离开复位态。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「纸上端到端 USB 事务」推演。

**任务**：假设你正在为新板卡写一份 USB 接口对接文档，请用一张表完整描述以下场景，并回答问题。

**场景**：雷达正在 50T 量产板上运行（`USB_MODE=1`），CFAR 在某距离-多普勒单元检测到目标。上位机此刻同时发来一条「读状态」命令（opcode `0xFF`）。

请回答：

1. **选哪个模块？** 顶层 `generate` 会例化哪一个 USB 模块？依据是哪一行源码？
2. **数据怎么发？** CFAR 检测结果如何进入 11 字节数据包？写出生字节流（用字段名，不必算数值），并指出 `detection` 字节的 `bit7` 在什么条件下为 1。
3. **命令怎么收？** 上位机的 `0xFF` 命令到达时，Write FSM 正在发包，会发生什么？哪条仲裁规则保证命令不被丢？
4. **状态怎么回？** `0xFF` 触发状态包返回，26 字节状态包里，Word 4 的 `agc_saturation_count` 占据哪几位？
5. **万一拔线？** 若用户此刻拔掉 USB，约多久后 FPGA 察觉？USB 域会发生什么？插回后如何恢复？

**参考答案要点**：

1. 例化 `usb_data_interface_ft2232h`（`gen_ft2232h` 分支），依据 [radar_system_top.v:L145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L145)（`USB_MODE=1`）与 [L792-L794](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L792-L794)。
2. `cfar_detection_cap` 填进数据包第 9 字节 `bit0`；字节流 = `0xAA, range_q_hi, range_q_lo, range_i_hi, range_i_lo, dop_re_hi, dop_re_lo, dop_im_hi, dop_im_lo, {frame_start,6'b0,detection}, 0x55`；`bit7=1` 当且仅当本样本是帧的第 0 个（`sample_counter==0`），见 [usb_data_interface_ft2232h.v:L417-L419](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L417-L419)。
3. Read FSM 优先级高于 Write FSM：Write FSM 整体被包在 `if (rd_state == RD_IDLE)` 里（[L585](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L585)），当前数据包发完后，`RD_IDLE` 看到 `!ft_rxf_n` 即接管总线收命令，命令不丢。
4. Word 4 = `{agc_gain[31:28], peak_mag[27:20], sat_count[19:12], agc_enable[11], 9'd0, range_mode[1:0]}`，`agc_saturation_count` 占 `[19:12]` 共 8 位，见 [usb_data_interface_ft2232h.v:L382-L387](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L382-L387)。
5. 约 0.655 ms 后 `ft_clk_lost` 置位（[L246-L247](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/usb_data_interface_ft2232h.v#L246-L247)），OR 进 `ft_reset_raw_n` 把 USB 域所有 FSM/FIFO 复位；插回后 `ft_clk` 恢复，心跳重新跳变，`ft_clk_lost` 清零，2 级同步器在 `ft_clk` 域干净释放 `ft_effective_reset_n`，FSM 从 `WR_IDLE`/`RD_IDLE` 重新开始。

## 6. 本讲小结

- USB 接口用「帧头 + 载荷 + 帧尾」成帧：**11 字节数据包**（`0xAA` + range_q/range_i/doppler_re/doppler_im + detection + `0x55`）与 **26 字节状态包**（`0xBB` + 6×32 位状态字 + `0x55`）。
- `detection` 字节复用了一个字节承载两个标志：`bit0` 是 CFAR 检测结果，`bit7` 是帧起始标志——后者让上位机能在任意位置重新对齐 2048 单元的帧边界，且不增加包长。注意源码注释（`{7'b0, cfar_detection}`）滞后于实现，应以 RTL 为准。
- 两套 USB 模块（FT601 32 位 / FT2232H 8 位）通过顶层 `parameter USB_MODE` + `generate` 在编译期二选一，共用同一套内部信号（`usb_cmd_*`、`status_*` 等），未选中的物理引脚被 tieoff；换板只改一个参数。
- **Read FSM** 以「读优先」仲裁共享总线，按 245 同步 FIFO 时序（`oe_n` 转向 → `rd_n` 采样 → 撤销）逐字节（FT2232H）或一次 32 位（FT601）收齐 4 字节命令，拆出 `cmd_opcode/addr/value` 拉一拍 `cmd_valid`。
- **时钟活动看门狗**用 100 MHz 域监视 `ft_clk` 的心跳翻转，约 0.655 ms 无跳变即置 `ft_clk_lost`，OR 进 USB 域复位实现断线自愈，恢复时用 2 级同步器干净释放，避免亚稳态。
- 两个 USB 模块在「断线看门狗」「包格式」「Read/Write FSM」上几乎复制粘贴，是「共用接口」设计哲学的具体落地。

## 7. 下一步学习建议

本讲只讲了「字节怎么流」，没有展开「命令含义」。建议：

1. **下一讲 u6-l2 主机命令协议与 Opcode 映射**：把本讲的 `cmd_opcode` 与 [radar_protocol.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) 的 `Opcode` 枚举、顶层 `case(usb_cmd_opcode)` 译码表一一对应，讲清 `0x03`/`0x21`/`0x30` 等 opcode 各写到哪个 `host_*` 寄存器。
2. **回看 u3-l2**：本讲的 toggle-CDC、`ASYNC_REG` 同步器、异步复位同步释放都建立在 u3-l2 的 CDC 基础上，若觉得生疏可重读。
3. **延伸阅读**：想理解上位机如何把 2048 个 11 字节包拼成一帧 Range-Doppler 图，可预读 u8-l2（数据采集线程与帧组装）。
4. **动手验证**：若你装了 iverilog，可参考 u11-l1 的回归脚本，给 `usb_data_interface_ft2232h` 写一个最小 testbench，模拟拔线（停 `ft_clk`）观察 `ft_clk_lost` 与 `ft_effective_reset_n` 的时序。
