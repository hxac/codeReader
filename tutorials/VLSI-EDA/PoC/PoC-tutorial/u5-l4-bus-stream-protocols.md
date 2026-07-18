# 总线与流式协议

## 1. 本讲目标

本讲进入 PoC 的 `PoC.bus` 命名空间，把前面单元学到的「命名空间包模式」「厂商选择机制」「FIFO」汇聚到一类新问题：**当多个模块要争抢同一资源、或要按统一的握手规则搬运数据帧时，PoC 提供了什么积木**。

学完本讲，读者应该能够：

1. 理解 `bus_Arbiter` 如何用「独热指针 + 优先级编码」实现 Round-Robin 仲裁，并知道 `STRATEGY` / `OUTPUT_REG` 等 generic 的作用。
2. 说清 PoC.Stream 流式协议的握手语义：`Valid` / `Ack`、`SOF` / `EOF` 成帧，以及它与 AXI-Stream 的对应关系。
3. 区分四类可综合流式 RTL 组件（`stream_Mux` / `stream_DeMux` / `stream_Buffer` / `stream_Mirror`）的职责，并理解它们内部如何复用仲裁逻辑与 FIFO。
4. 知道 `stream_Source` 是仿真专用的总线功能模型（BFM），并能够编写一个最小的数据回环测试台。
5. 了解 `PoC.bus.wb` 子命名空间对 Wishbone 总线的接入规划，并清楚它在当前快照中的实现状态。

## 2. 前置知识

本讲假定你已经掌握以下概念（来自前面单元），这里只做一句话回顾：

- **命名空间包模式（u3-l1）**：每个命名空间有一份 `<ns>.pkg.vhdl`「根包」，集中声明 component / type / function，必须先于具体核编译。
- **generic + `if generate`（u3-l2）**：PoC 用 generic（含字符串 generic）在展开期选择实现路径，未覆盖取值用 `assert ... severity FAILURE` 兜底。
- **FIFO 家族（u3-l4）**：写侧 `put/din/full`、读侧 `got/dout/valid`，`fifo_cc_got` 是同钟主力、`fifo_glue` 是 2 字深度解耦器，`fifo_cc_got_tempgot` 支持暂存回滚（commit/rollback）。
- **辅助函数（u2-l2）**：`log2ceilnz`、`ite`、`onehot2bin`、`to_slv` 等；以及 `vectors` 包里的二维位矩阵 `T_SLM` 与 `get_row` / `assign_row`。
- **测试台骨架（u4-l1 / u4-l2）**：`simInitialize` / `simGenerateClock` 三连调用，含 `wait` 的过程裸写在架构体里即等价隐式并发进程。

如果你对上面任何一项还陌生，建议先回看对应讲义。本讲不再重复这些基础，直接在它们之上讨论总线与流。

此外补充三个本讲会用到的硬件设计常识：

- **总线仲裁（Arbitration）**：当多个请求方（master）要访问同一个共享资源（总线、存储口、发送通道）时，需要一个仲裁器决定「这一拍把资源授权给谁」。仲裁策略决定了公平性与延迟，最常见的是 **Round-Robin（轮询）**：轮流给每个请求方机会，避免某个请求方饿死。
- **握手协议（Handshake）**：数据从源头搬到目的地，最稳健的方式是双方各举一根控制线——源头说「我准备好了」（`Valid`），目的地有能力时回一句「我收下了」（`Ack` / `Ready`）。只有两边同时为 1 的那一拍，数据才算真正搬走。
- **成帧（Framing）**：一连串数据字组成一个「帧」（frame），例如一个网络包、一行图像。用 `SOF`（Start Of Frame）标记帧的第一个字、`EOF`（End Of Frame）标记最后一个字，下游就能知道一个完整事务的边界。

## 3. 本讲源码地图

本讲涉及的关键文件如下（注意：下面会标注部分文件在当前快照中的真实存在状态，避免你照着 README 去找一个并不存在的文件）：

| 文件 | 作用 | 是否存在 |
| --- | --- | --- |
| [src/bus/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/README.md) | `PoC.bus` 命名空间总说明，列出子命名空间与实体清单 | ✅ |
| [src/bus/bus_Arbiter.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/bus_Arbiter.vhdl) | 通用仲裁器，本讲核心 | ✅ |
| [src/bus/stream/stream.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream.pkg.vhdl) | stream 命名空间根包：仿真用流字记录与 `dat/sof/eof/eofg` 构造函数 | ✅ |
| [src/bus/stream/stream_Mux.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mux.vhdl) | 多路输入→单路输出的流复用器（内含仲裁 + FSM） | ✅ |
| [src/bus/stream/stream_DeMux.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_DeMux.vhdl) | 单路输入→多路输出的流解复用器（含丢帧态） | ✅ |
| [src/bus/stream/stream_Buffer.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Buffer.vhdl) | 基于多 FIFO 的弹性缓冲，支持按帧 commit/rollback | ✅ |
| [src/bus/stream/stream_Mirror.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mirror.vhdl) | 一进多出的「镜像/广播」组件，可作回环验证的近邻 | ✅ |
| [src/bus/stream/stream_Source.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Source.vhdl) | **仿真专用**数据源 BFM，按帧组驱动流接口 | ✅ |
| [src/bus/wb/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/wb/README.md) | Wishbone 子命名空间说明（仅文档） | ✅ |

> ⚠️ **重要事实核对**：在当前 HEAD（`8c39b24`）快照下，有几处「README 提到但源码尚未落地」的情况，本讲会如实说明，不会假装它们存在：
>
> - `src/bus/stream/stream_Sink.vhdl`：README 列为「generic data sink for simulation」，但仓库里**没有**对应 `.vhdl` / `.files`，尚未实现。
> - `src/bus/bus.pkg.vhdl`：`bus_Arbiter.files` 里这一行是**被注释掉**的，仓库里也无此文件；`bus_Arbiter` 直接以 `use PoC.utils.all` 引用公共包。
> - `src/bus/wb/wb_fifo_adapter.vhdl` / `wb_ocram.vhdl` / `wb_uart_wrapper.vhdl`：wb 子命名空间**只有 README.md**，三个 Wishbone 适配器均未实现。
>
> 因此本讲的「Wishbone 适配」一节会以 README 的设计意图 + Wishbone 协议常识来讲，并明确标注「待实现」。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：仲裁器、流式协议握手、流式 RTL 组件、仿真 BFM 与回环验证、Wishbone 适配。

### 4.1 总线仲裁：bus_Arbiter

#### 4.1.1 概念说明

当多个请求方要访问同一资源时，仲裁器（Arbiter）是「裁判」：每拍收下所有请求方的 `Request_Vector`，输出一个授权信号——告诉资源「这一拍听谁的」。仲裁器不搬数据，它只做**调度决策**。

仲裁策略决定公平性：

- **Round-Robin（RR，轮询）**：维护一个「指针」指向上次授权的请求方，下一次从指针的下一格开始找第一个有请求的端口，保证每个端口都有机会。
- **Lottery（LOT，彩票/加权）**：按权重 `WEIGHTS` 随机抽签（`bus_Arbiter` 的 generic 已经为此预留，但实现被注释掉，见后文）。

PoC 的 `bus_Arbiter` 是一个**纯控制**模块：输入是请求位向量，输出是授权位向量（one-hot）与授权序号（二进制），不触碰任何数据通路。

#### 4.1.2 核心流程

`bus_Arbiter` 的核心是一个**独热（one-hot）轮转指针**，用位运算「一步」算出下一个授权端口，而不是写一个循环逐位扫描。直觉如下：

1. 维护寄存器 `ChannelPointer_d`（one-hot，初值为最低端口 `to_slv(1, PORTS)`），表示「当前轮到谁」。
2. 每来一次 `Arbitrate` 脉冲，计算「在当前指针**之前**还有没有请求」（`RequestLeft`），从而决定是从「指针之前」挑下一个，还是从「全体的最左」绕回来。
3. 用 `(not X) + 1` 这个位运算技巧（在 one-hot 上等价于「提取最低位的 1」）一次性得到下一个授权端口的 one-hot 码。
4. 把 one-hot 转 2 进制（`onehot2bin`）得到 `Grant_Index`，方便外部直接当数组下标用。

`OUTPUT_REG` generic 控制**输出是否再寄存一拍**：`FALSE` 时授权组合输出（快、但时序路径长）；`TRUE` 时授权寄存输出（慢一拍、但时序干净）。这与 FIFO 家族的 `OUTPUT_REG`（u3-l4）是同一个设计思想。

伪代码：

```
每拍:
  if Reset:  ChannelPointer_d <= 最低端口
  elif Arbitrate:
      RequestLeft   := 在指针之前的请求   # 屏蔽指针及之后
      if RequestLeft 非全 0:
          nxt := 提取 RequestLeft 最低 1 位        # 指针之前还有，就近选
      else:
          nxt := 提取 RequestVector 最低 1 位      # 之前没了，从头绕回
      ChannelPointer_d <= nxt
  Grant_Vector <= nxt        # one-hot 授权
  Grant_Index  <= onehot2bin(nxt)
```

#### 4.1.3 源码精读

实体声明只有控制端口，没有数据端口。`STRATEGY` 是字符串 generic，`PORTS` 决定向量宽度，`Grant_Index` 的位宽用 `log2ceilnz(PORTS)` 推导：

[bus_Arbiter.vhdl:42-60](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/bus_Arbiter.vhdl#L42-L60) — 实体与 generic/port 声明。注意 `log2ceilnz`（来自 `PoC.utils`，见 u2-l2）保证 `PORTS=1` 时位宽不为 0。

入口处用 `assert` 守卫未知策略字符串，与 u3-l2 讲的「未覆盖取值用 assert FAILURE 兜底」一致：

[bus_Arbiter.vhdl:71-72](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/bus_Arbiter.vhdl#L71-L72) — 只接受 `"RR"` 与 `"LOT"` 两种策略。

仲裁核心的三行位运算。关键是 `(not X) + 1` 在 one-hot 向量上的语义——它把「求最低有效 1」这件事变成了普通二进制加法：

[bus_Arbiter.vhdl:90-93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/bus_Arbiter.vhdl#L90-L93) — `RequestLeft` 屏蔽出「指针之前」的请求；`SelectLeft`/`SelectRight` 分别求「指针之前的最低 1」与「全体的最低 1」；`ite` 据前者是否为 0 二选一，得到下一个指针 `ChannelPointer_nxt`。

`OUTPUT_REG` 用 `if generate` 二选一地产生寄存器与输出。`genREG0`（不寄存输出，组合授权）和 `genREG1`（寄存输出）各持有一份时钟进程；两者只会展开一个：

[bus_Arbiter.vhdl:96-111](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/bus_Arbiter.vhdl#L96-L111) — `genREG0` 分支：指针寄存、授权组合输出，`Grant_Index` 由 `onehot2bin` 当拍算出。

文件末尾的 Lottery 分支被整段注释，说明 LOT 目前是「占位」：

[bus_Arbiter.vhdl:136-141](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/bus_Arbiter.vhdl#L136-L141) — `genLOT` 只有空壳，所以实际只有 RR 可用。

#### 4.1.4 代码实践

**实践目标**：通过修改 generic，观察 Round-Robin 仲裁的授权轮转顺序。

**操作步骤**（源码阅读型实践，不修改源码）：

1. 阅读 [bus_Arbiter.vhdl:88-110](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/bus_Arbiter.vhdl#L88-L110)，假设实例化为 `PORTS => 4`、`OUTPUT_REG => FALSE`。
2. 设想 `Request_Vector` 一直保持 `"1111"`（4 个端口同时请求），`Arbitrate` 每拍为 1，`ChannelPointer_d` 初值为 `"0001"`。
3. 在纸上逐拍推演 `ChannelPointer_nxt` 与 `Grant_Index`。

**需要观察的现象**：

- 指针 one-hot 应当在 `0001 → 0010 → 0100 → 1000 → 0001 …` 之间循环（最低位优先）。
- `Grant_Index` 对应 `0 → 1 → 2 → 3 → 0 …`。
- 如果只有端口 0 和端口 2 请求（`"0101"`），指针应只在两者间轮转，跳过无请求的端口 1、3。

**预期结果**：4 拍一个完整轮次，每个有请求的端口都不会被跳过两次以上——这正是 Round-Robin 的「不饿死」保证。

> 运行结果：**待本地验证**（本讲未执行仿真；若要实测，可仿照 u4-l2 的测试台骨架，把 `bus_Arbiter` 当 DUT，用 `simGenerateClock` 产生时钟，进程里按拍驱动 `Request_Vector` 并打印 `Grant_Index`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Grant_Index` 的位宽用 `log2ceilnz(PORTS)` 而不是 `log2ceil(PORTS)`？

**参考答案**：当 `PORTS = 1` 时，`log2ceil(1) = 0`，会得到 0 位的 `std_logic_vector`，VHDL 不允许 0 位向量声明。`log2ceilnz`（no-zero）保证最小位宽为 1（见 u2-l2），使 `PORTS=1` 的退化情况也能综合。

**练习 2**：把 `OUTPUT_REG` 从 `FALSE` 改成 `TRUE`，输出时序会发生什么变化？

**参考答案**：授权结果（`Arbitrated` / `Grant_Vector` / `Grant_Index`）会比组合版本晚一拍出现（见 `genREG1` 分支用 `ChannelPointer_d` 与 `ChannelPointer_bin_d` 作寄存输出）。好处是去掉了 `onehot2bin` 这条组合长路径，时序更干净；代价是授权延迟 +1 拍。

**练习 3**：`RequestLeft` 那行 `(not ((unsigned(ChannelPointer_d) - 1) or unsigned(ChannelPointer_d)))` 想屏蔽掉「指针及其之后」的端口，请用 `PORTS=4`、`ChannelPointer_d="0100"`（指针指向端口 2）验证它确实只保留了端口 0、1。

**参考答案**：`ChannelPointer_d - 1 = "0011"`，`"0100" or "0011" = "0111"`，`not "0111" = "1000"`。即掩码是 `"1000"`，只保留端口 3 之前……注意这里「指针之前」的方向与下标高低有关，实际行为以仿真为准（**待本地验证**）；核心要点是：它用一个算术减法 + 按位或 + 取反，构造出「指针之前/之后」的掩码，避免显式循环。

---

### 4.2 PoC.Stream 流式协议：握手与成帧

#### 4.2.1 概念说明

仲裁器解决「谁先来」，而流式协议解决「数据怎么搬」。PoC 在 `PoC.bus.stream` 下定义了一套自家的流式接口，称为 **PoC.Stream**。它和业界熟悉的 **AXI-Stream（AXIS）** 思想一致——都是「源端驱动 valid，目的端回 ready/ack，二者同时有效才搬一字」——但命名与成帧信号略有不同。

对照表：

| 概念 | AXI-Stream | PoC.Stream |
| --- | --- | --- |
| 数据有效 | `TVALID` | `Valid` |
| 目的端可收 | `TDATA` + `TREADY` | `Data` + **`Ack`**（Ack 即「收下了」，等价于 ready） |
| 帧结束 | `TLAST` | `EOF`（End Of Frame） |
| 帧开始 | （通常无专用信号） | `SOF`（Start Of Frame） |
| 边带数据 | `TUSER` / `TID` / `TDEST` | `Meta`（metadata，按比特分段） |

所以理解 PoC.Stream 的关键就两句话：

1. **一拍数据搬运的成立条件**：`Valid = '1'` 且 `Ack = '1'`。
2. **一帧的边界**：`SOF = '1'` 的那一拍是帧首，`EOF = '1'` 的那一拍是帧尾；中间的拍是帧体。`SOF` 与 `EOF` 可以在同一拍（单字帧）。

PoC 把方向也讲清楚了——源端（Source）一侧的握手信号叫 `Out_*`，目的端（Sink）一侧叫 `In_*`。例如 `stream_Source` 输出 `Out_Valid`/`Out_Data`/`Out_SOF`/`Out_EOF`，并读回 `Out_Ack`；而 `stream_Mux` 的输入侧是 `In_Valid`/`In_Ack`（`In_Ack` 是输出，表示「我收下了」）。

#### 4.2.2 核心流程

一次成功的数据搬运时序（以源端视角）：

```
时钟沿   :   ┌────╲────╱────╲────╱────╲────╱────╲
              │    ╲   ╱    ╲   ╱    ╲   ╱    ╲
Out_Valid:    ────── D0置1 ────── D1置1 ────── ...
Out_Data :           D0            D1
Out_Ack  :    ──────────── 置1 ──────── 置1 ── ...
                              ↑               ↑
                     D0 在此拍被收下   D1 在此拍被收下
```

握手规则：

- 源端拉高 `Valid` 并给出 `Data`，**保持稳定**直到被收下。
- 目的端准备好时拉高 `Ack`。
- 在某个上升沿，若 `Valid='1'` 且 `Ack='1'`，该字算「搬走」，源端才能前进到下一字。
- `SOF`/`EOF` 跟随对应的数据字一起出现，目的端在收到 `EOF` 字时就知道一帧结束。

注意 PoC.Stream 在不同组件里对 `Ack` 的极性与采样细节略有差异（仿真 BFM 在下降沿采样 `Ack`，RTL 组件在上升沿寄存），但「`Valid & Ack` 同拍有效即成交」这条铁律不变。

#### 4.2.3 源码精读

`stream.pkg.vhdl` 定义了一个**仿真用**的「流字」记录，把一次握手的所有边带信息打包成一个对象，方便测试台描述激励：

[stream.pkg.vhdl:44-51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream.pkg.vhdl#L44-L51) — `T_SIM_STREAM_WORD_8` 记录含 `Valid`/`Data`/`SOF`/`EOF`/`Ready`/`EOFG` 六个字段。`EOFG`（End Of Frame Group）是比 EOF 更高一层的「一组测试帧结束」标记，供 `stream_Source` 判断是否跑完所有激励。

包里提供了一组构造函数（`dat`/`sof`/`eof`/`eofg`），让你像写脚本一样构造帧序列，而不是手填每个字段：

[stream.pkg.vhdl:135-152](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream.pkg.vhdl#L135-L152) — 这些重载函数接受位宽为 8 或 32 的标量/向量，返回带好 `SOF`/`EOF`/`EOFG` 标记的流字或流字数组。例如 `sof(x)` 把第一字标 SOF，`eof(x)` 把最后一字标 EOF，`eofg(x)` 在末尾追加 EOFG。

> 这个包是纯仿真用的（用到 `report`、字符串、大数组），但它**也**承担「命名空间根包」的角色——`stream_Mux.files` / `stream_Source.files` 等都先编译它，再编译具体实体（见 [stream_Source.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Source.files)）。也就是说，可综合的 `stream_Mux` 和仿真的 `stream_Source` 共用同一个 `use PoC.stream.all;`（注意包名是 `stream`，不是 `bus.stream`——文件路径是 `src/bus/stream/`，但 VHDL 包名直接是 `stream`）。

#### 4.2.4 代码实践

**实践目标**：用包里的构造函数手写一个 3 字帧的激励向量，理解 SOF/EOF/EOFG 的位置。

**操作步骤**（源码阅读 + 手写激励型实践）：

1. 阅读 [stream.pkg.vhdl:223-239](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream.pkg.vhdl#L223-L239) 的 `sof(slvv)` 与 [stream.pkg.vhdl:259-275](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream.pkg.vhdl#L259-L275) 的 `eof(slvv)`。
2. 在脑中（或一张纸上）构造：

   ```vhdl
   -- 示例代码（非项目原有代码，仅供理解）
   constant FRAME : T_SIM_STREAM_WORD_VECTOR_8 :=
       eofg(eof(sof(T_SLVV_8'((x"01", x"02", x"03")))));
   ```

3. 逐字写下 `FRAME(0)`、`FRAME(1)`、`FRAME(2)` 各自的 `SOF`/`EOF`/`EOFG` 取值。

**需要观察的现象**：

- `FRAME(0)`：`SOF='1'`，`EOF='0'`，`EOFG=FALSE`（帧首，由 `sof` 设置）。
- `FRAME(1)`：全 0 / FALSE（帧体）。
- `FRAME(2)`：`SOF='0'`，`EOF='1'`，`EOFG=TRUE`（帧尾 + 整组结束，由 `eof` 再 `eofg` 叠加）。

**预期结果**：这 3 个字恰好描述「一个完整帧，且是整组激励的最后一帧」。这正是 `stream_Source` 会按顺序吐出的内容（见 4.4 节）。

> 运行结果：**待本地验证**（可在测试台里 `report to_string(FRAME(2))` 打印，`to_string` 见 [stream.pkg.vhdl:421-424](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream.pkg.vhdl#L421-L424)，会输出类似 `RV 0x03 EOF*` 的可读串）。

#### 4.2.5 小练习与答案

**练习 1**：PoC.Stream 用 `Ack`，AXI-Stream 用 `TREADY`，二者在握手成立条件上是否一致？

**参考答案**：一致。两者都要求「源端 valid」与「目的端 ready/ack」在同一拍同时有效，数据才算搬运成功。命名差异不影响协议本质；本讲后续统一用「`Valid & Ack`」描述 PoC.Stream 的成交条件。

**练习 2**：为什么 PoC.Stream 专门设了 `SOF`，而 AXI-Stream 通常没有？

**参考答案**：AXI-Stream 只用 `TLAST` 标记帧尾，帧首要靠接收侧「第一次见到 valid」来自行推断。PoC.Stream 显式给 `SOF`，让接收侧（如 `stream_DeMux`）可以在帧首那一拍就**决定整帧路由到哪个出口**，而不必等到帧中间再改路——这对基于帧的路由/仲裁（见 4.3）更方便。

**练习 3**：`EOFG` 和 `EOF` 有什么区别？

**参考答案**：`EOF` 标记「一个帧」的结束；`EOFG`（End Of Frame Group）标记「一组测试帧」的结束，是仿真激励层面的概念。`stream_Source` 用 `EOFG` 判断「当前测试用例的激励是否全部发完」，从而跳到下一个 testcase（见 4.4.3）。

---

### 4.3 流式组件：Mux / DeMux / Buffer / Mirror

#### 4.3.1 概念说明

仲裁器解决了「谁先来」之后，你往往还需要把这些数据**搬运、合并、分拆、缓存**。`PoC.bus.stream` 提供四个可综合的 RTL 组件来搭数据通路：

| 组件 | 拓扑 | 职责 |
| --- | --- | --- |
| `stream_Mux` | N 进 1 出 | 多个输入流按帧轮流选通到单输出（**内含仲裁器 + FSM**） |
| `stream_DeMux` | 1 进 N 出 | 单输入流按帧路由到指定输出，或丢弃（含丢帧态） |
| `stream_Buffer` | 1 进 1 出 | 弹性缓冲（FIFO），按帧 commit/rollback，支持边带 Meta |
| `stream_Mirror` | 1 进 N 出 | 一进多出「广播/镜像」，把同一帧送给多个出口 |

这四个组件都用 PoC.Stream 接口（`Valid`/`Ack`/`SOF`/`EOF`/`Meta`），因此可以像搭积木一样串起来：`Mux → Buffer → DeMux`。它们的共同特点是——**以帧为单位**做决策（在 `SOF` 那拍锁定一个通道，直到 `EOF` 才释放），而不是逐字切换，保证一帧数据不会被劈成两半。

> 本小节聚焦最具代表性的 `stream_Mux` 与 `stream_DeMux`（它们直接复用 4.1 的仲裁思想），并简要说明 `stream_Buffer`/`stream_Mirror`。

#### 4.3.2 核心流程

**`stream_Mux`（多路复用）** 的核心是一个 2 态 FSM + 一个轮转指针：

1. `ST_IDLE`：监听所有输入端口的 `In_Valid & In_SOF`（只有带 SOF 的字才算「一个帧请求」）。一旦有请求，按 Round-Robin 选一个端口，锁定指针，进入 `ST_DATAFLOW`。
2. `ST_DATAFLOW`：被选中端口的 `Data/SOF/EOF` 直通到输出，输出 `Out_Valid` 由 FSM 使能。当输出收到 `EOF` 且被 `Out_Ack` 收下时，检查「还有没有别的端口在请求」——有则切到下一个端口，没有则回 `ST_IDLE`。

要点：

- **仲裁按帧**：`RequestVector <= In_Valid and In_SOF`，只在帧首那一拍仲裁，整帧期间锁定同一端口，不会把一帧拆给两个出口。
- **背压传递**：输出的 `Out_Ack`（来自下游）只在被选中端口上回传为 `In_Ack`，其余端口 `In_Ack='0'`（它们在排队等）。
- **轮转指针**与 `bus_Arbiter` 用的是**同一套** `(not X)+1` 位运算。

**`stream_DeMux`（多路解复用）** 多了一个 `ST_DISCARD_FRAME` 态：当控制向量 `DeMuxControl` 全 0（没有任何出口想要这一帧）时，进入丢帧态，整帧吃掉但不出到任何端口，直到 `EOF` 才回 `ST_IDLE`。

#### 4.3.3 源码精读

`stream_Mux` 的 generics 用 `DATA_BITS`/`META_BITS` 描述数据与边带宽度，`PORTS` 描述输入端口数；端口用二维矩阵 `T_SLM`（来自 `PoC.vectors`，见 u2-l4）来传递「每个端口一行的数据」：

[stream_Mux.vhdl:41-68](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mux.vhdl#L41-L68) — 注意输入侧 `In_Valid`/`In_Data`/`In_SOF`/`In_EOF` 都是按端口分量的，`In_Ack` 是输出（背压）。`META_REV_BITS` 是「反向 meta」通道，用于把下游的边带信息回传给被选中的输入端口。

仲裁逻辑与 `bus_Arbiter` 同源——同样的 `RequestLeft`/`SelectLeft`/`SelectRight` 三件套：

[stream_Mux.vhdl:160-166](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mux.vhdl#L160-L166) — 复用了 4.1.3 的位运算仲裁，把结果转成二进制 `idx`，供 `get_row` 从输入矩阵中取出被选中端口的数据。

输出与背压的接线：

[stream_Mux.vhdl:168-176](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mux.vhdl#L168-L176) — `Out_Data`/`Out_Meta` 用 `get_row` 按选中下标取行；`Out_Valid` 只有在 `ST_DATAFLOW` 态（`FSM_Dataflow_en`）才拉高；`In_Ack` 只回给被选中的端口（与 one-hot `ChannelPointer` 相与）。这正是「按帧锁定 + 背压只传被选端口」的实现。

`stream_DeMux` 的三态 FSM 多了一个丢弃态：

[stream_DeMux.vhdl:79-80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_DeMux.vhdl#L79-L80) — `ST_DISCARD_FRAME`。当所有出口都不想要这帧时进入此态。

[stream_DeMux.vhdl:103-104](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_DeMux.vhdl#L103-L104) — `DiscardFrame <= slv_nor(DeMuxControl)`：控制向量全 0 即丢弃。

`stream_Buffer` 把 PoC 的 FIFO 家族（u3-l4）包装成一个「按帧提交」的弹性缓冲：数据进一个 `fifo_cc_got`，meta 进若干个 `fifo_cc_got_tempgot`（支持 commit/rollback），只有当一帧的 `EOF` 被下游收下时才 commit，否则可整帧回滚：

[stream_Buffer.vhdl:187-213](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Buffer.vhdl#L187-L213) — 数据 FIFO 实例化 `fifo_cc_got`，把 `DATA_BITS` 数据 + 1 位 EOF 标记拼成 `DATA_BITS+1` 位宽存入。

[stream_Buffer.vhdl:215](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Buffer.vhdl#L215) — `FrameCommit` 仅在「FIFO 有数据 + 当前是 EOF + 下游 Ack」同时成立时拉高，作为所有 meta FIFO 的 commit 信号。

`stream_Mirror` 则更简单：用一个 2 字深度的 `fifo_glue`（u3-l4）做一拍解耦，把同一份数据广播到所有 `PORTS` 个出口，并用 `Mask_r` 记录「哪些出口还没收下」，等所有（或被使能的）出口都 Ack 了才推进——这正是「广播要等所有接收者」的典型实现。

#### 4.3.4 代码实践

**实践目标**：阅读 `stream_Mux` 的 FSM，画出两个输入端口各发一帧时的输出时序。

**操作步骤**（源码阅读型实践）：

1. 阅读 [stream_Mux.vhdl:108-147](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mux.vhdl#L108-L147) 的两个进程（状态寄存进程 + 组合下一态进程）。
2. 假设 `PORTS=2`，端口 0 发一帧 `[A0(A,B)]`（SOF 在 A，EOF 在 B），端口 1 同时发一帧 `[C0(C,D)]`。
3. 设 `ChannelPointer_d` 初值指向端口 1（最高优先，`to_slv(2**(PORTS-1), PORTS)`，见 [stream_Mux.vhdl:95](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mux.vhdl#L95)）。

**需要观察的现象**：

- 在两个端口的 SOF 同时到达时，FSM 先选中端口 1，输出 `C0, C1`（端口 1 的整帧）。
- 端口 1 的 `EOF` 被 Ack 后，由于端口 0 还在请求（`RequestWithoutSelf='1'`），指针切到端口 0，紧接着输出 `A0, A1`。
- 全程端口 0 的 `In_Ack` 在端口 1 占线期间为 0，等被选中后才变 1。

**预期结果**：输出流为 `C0, C1, A0, A1`（端口 1 先、端口 0 后），每帧完整连续，不会被交错。这验证了「按帧仲裁、整帧锁定」。

> 运行结果：**待本地验证**（若实测，需用 `stream_Source` 之类 BFM 喂两个输入端口；但本快照无 `stream_Sink`，下游只能手写消费进程）。

#### 4.3.5 小练习与答案

**练习 1**：`stream_Mux` 为什么用 `In_Valid and In_SOF` 作为请求，而不是直接用 `In_Valid`？

**参考答案**：因为仲裁以**帧**为单位。只在帧首（SOF）那一拍参与仲裁，整帧期间锁定同一端口；若用 `In_Valid` 逐字仲裁，就可能在一帧中间切换端口，把一帧劈成两半送到下游，破坏帧完整性。

**练习 2**：`stream_DeMux` 的 `ST_DISCARD_FRAME` 态解决了什么问题？

**参考答案**：当控制向量 `DeMuxControl` 全 0（没有任何出口要这帧）时，不能简单地把帧扔在输入口堵住后续帧。`ST_DISCARD_FRAME` 让 DeMux 继续对输入回 `In_Ack='1'`（吃掉这些字），但不对任何出口输出，直到 `EOF` 才回到 `ST_IDLE`——即「优雅地丢弃整帧」而不阻塞上游。

**练习 3**：`stream_Buffer` 的数据 FIFO 存的是 `DATA_BITS+1` 位，多出的那 1 位是什么？

**参考答案**：是 `EOF` 标记（见 `EOF_BIT` 与 `DataFIFO_DataIn(EOF_BIT) <= In_EOF`）。把 EOF 跟数据一起存进 FIFO，下游读出时就能从数据流里恢复帧边界，从而在「读到 EOF 字且被 Ack」时触发 `FrameCommit`，实现按帧提交。

---

### 4.4 仿真 BFM：stream_Source 与数据回环

#### 4.4.1 概念说明

流式组件是可综合 RTL，但在测试台里你需要一个「会自动发数据」的源端和「会自动收数据」的目的端来驱动它们。这类专门为仿真写的、模拟某条总线协议的模型，叫 **总线功能模型（Bus Functional Model, BFM）**。

PoC.Stream 的 BFM 有两个（README 里列了 `stream_Source` 与 `stream_Sink`）：

- `stream_Source`：**已实现**。仿真源端，按你给的帧组（`TESTCASES`）顺序，在 `Enable` 拉高后自动把帧字一个个吐到 `Out_*` 接口，并按 `Out_Ack` 决定何时前进。
- `stream_Sink`：**当前快照未实现**（README 列出但无源码，见第 3 节事实核对）。所以本讲的回环实践里，目的端由你自己写一个消费进程来充当 sink。

> BFM 的本质：它用 `process` + `wait until rising_edge(Clock)` 这种不可综合的写法，把「如何按协议发数据」的繁琐时序封装起来，让你在测试台里只需描述「发哪些帧」。

#### 4.4.2 核心流程

`stream_Source` 的驱动循环（简化）：

```
wait until Enable='1'; wait until rising_edge(Clock);
for 每个 testcase in TESTCASES:
    跳过 Active=FALSE 的用例
    等 PrePause 个时钟
    loop:
        wait until rising_edge(Clock)
        把 Data[WordIndex] 的 Valid/Data/SOF/EOF 驱到 Out_*
        wait until falling_edge(Clock)
        if Out_Ack='1':  WordIndex++        # 被下游收下才前进
        当上一字是 EOFG 时退出 loop
    等 PostPause 个时钟
恢复接口默认值
```

要点：

- 在**上升沿后**驱动输出（保证整周期稳定），在**下降沿**采样 `Out_Ack`（给组合/寄存的下游留出半个周期稳定时间）——这是 BFM 常见的「半周期裕量」写法。
- 只有 `Out_Ack='1'` 才让 `WordIndex` 前进，正好实现「`Valid & Ack` 成交才搬字」的握手。
- `MAX_CYCLES` 是看门狗：单个 testcase 跑太久就 `severity FAILURE` 报错，防止下游永远不 Ack 把仿真挂死。

#### 4.4.3 源码精读

实体声明：只有一个 generic `TESTCASES`（帧组向量），端口是标准的 PoC.Stream 源端 + 一个 `Enable` 控制位：

[stream_Source.vhdl:42-58](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Source.vhdl#L42-L58) — 注意它**没有时钟/波特率分频 generic**（对比 u3-l7 的 `uart_bclk`），因为它运行在测试台提供的时钟域里。

握手驱动的关键 6 行：

[stream_Source.vhdl:120-131](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Source.vhdl#L120-L131) — 上升沿驱动输出、下降沿采样 `Out_Ack`，`Ack='1'` 才 `WordIndex+1`。这就是 PoC.Stream 源端握手的「活样本」。

看门狗与用例切换：

[stream_Source.vhdl:114-136](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Source.vhdl#L114-L136) — `MAX_CYCLES` 看门狗；`exit when ... EOFG=TRUE` 判定整组激励结束。

#### 4.4.4 代码实践（本讲主实践：数据回环）

**实践目标**：用 `stream_Source` 驱动一个 PoC.Stream 接口的 DUT，并在下游用一个**手写的消费进程**充当 sink（因当前快照无 `stream_Sink`），构成一个数据回环，观察 `Valid`/`Ack` 握手时序。

**操作步骤**（编写最小测试台型实践）：

1. 在测试台里实例化 `stream_Source`，给一个单帧、3 字的 `TESTCASES` 常量（用 4.2.4 构造的 `FRAME`）。
2. 把 `stream_Source` 的 `Out_*` 直接连到一个手写的消费进程（充当 sink），中间可以先不插 DUT（纯回环验证握手），熟练后再插入 `stream_Buffer` 或 `stream_Mux`。
3. 手写 sink 进程：每拍都愿意收（`Out_Ack <= '1'`），在上升沿 `if Out_Valid='1' then report ...` 打印收到的字，统计收到的字数与帧尾。

   ```vhdl
   -- 示例代码（非项目原有代码，sink 进程骨架）
   process
   begin
      Out_Ack <= '1';                       -- 总是可收（理想 sink）
      wait until rising_edge(Clock);
      if Out_Valid = '1' then
         report "recv: " & to_string(Out_Data);   -- 记录收到的字
         if Out_EOF = '1' then
            report "frame end";              -- 帧边界
         end if;
      end if;
   end process;
   ```

4. 用 u4-l1 的 `simGenerateClock` 产生时钟、`simGenerateWaveform` 产生复位与 `Enable`。

**需要观察的现象**：

- `Enable` 拉高后，`Out_Valid` 在帧的 3 个字期间依次为 1。
- 因为 sink 每拍都回 `Out_Ack='1'`，`stream_Source` 每拍前进一字，3 拍发完一帧。
- 若把 sink 改成「每 2 拍才回一次 Ack」，应观察到 `Out_Valid` 保持高、数据不变，直到 `Out_Ack` 出现才前进——这就是背压。
- 收字总数应为 3，最后带 `EOF`。

**预期结果**：回环把 3 个字 `0x01, 0x02, 0x03` 原样收到，首字带 SOF、末字带 EOF；握手严格遵循「`Valid & Ack` 同拍有效即成交」。

> 运行结果：**待本地验证**（本讲未执行仿真。注意：`stream_Source` 用了 `assert ... severity WARNING` 打印大量调试信息，仿真日志会较吵；如需插入真实 DUT，`stream_Buffer` 是最简单的 1 进 1 出选择，见 4.3）。

#### 4.4.5 小练习与答案

**练习 1**：`stream_Source` 为什么在下降沿采样 `Out_Ack`，而不是上升沿？

**参考答案**：源端在上升沿后驱动 `Out_*`，下游（sink 或 RTL）通常在上升沿寄存 `Out_Ack`。若源端也在上升沿采样，可能与下游在同一沿抢更新造成竞争；改在下降沿采样，给下游整整半个周期让 `Out_Ack` 稳定下来，再由源端读取——这是 BFM 常用的安全裕量写法。

**练习 2**：把 sink 进程的 `Out_Ack` 永远置 1，与「每 3 拍才置 1」相比，`stream_Source` 发完同一帧各需多少拍？

**参考答案**：理想 sink 下，每字 1 拍，3 字帧需 3 拍成交。若每 3 拍才 Ack 一次，则每字要等 3 拍才被收下，3 字帧约需 9 拍。背压直接拉长发包时间，这正是流式握手「下游慢则上游等」的体现。

**练习 3**：为什么 `stream_Source` 不能用于上板综合？

**参考答案**：它用了 `process` + `wait until rising_edge(Clock)`、`wait until falling_edge(...)`、`assert ... report`、动态 `TESTCASES` 数组等不可综合结构，是纯仿真模型。综合的源端应由真实硬件（如 DMA、计数器读 RAM）驱动同样的 `Out_*` 接口。

---

### 4.5 Wishbone 适配：PoC.bus.wb

#### 4.5.1 概念说明

**Wishbone** 是 OpenCores 社区提出的一套开源片上总线协议，常用于把一个 SoC 里的 master（如 CPU）和 slave（如内存控制器、UART、自定义 IP）连起来。它的核心也是握手式：master 发 `CYC_O`（周期）/`STB_O`（选通），slave 回 `ACK_O`（应答），数据在 `DAT_O`/`DAT_I` 上搬运。

PoC 的 `PoC.bus.wb` 子命名空间的**意图**是：把 PoC 的内部接口（PoC.Stream、FIFO、ocram）适配到 Wishbone 总线上，让 PoC 核能挂进 Wishbone SoC。README 规划了三个适配器：

- `wb_fifo_adapter`：FIFO ↔ Wishbone 桥。
- `wb_ocram`：ocram（片上 RAM，见 u3-l3）的 Wishbone 封装。
- `wb_uart_wrapper`：把 PoC 的 UART 收发核（u3-l7）包成 Wishbone slave。

但**需要再次强调**：在当前快照（HEAD `8c39b24`）下，`src/bus/wb/` 目录里**只有 `README.md`**，这三个适配器的 `.vhdl` 源码**都尚未实现**。所以本节讲的是「设计意图 + Wishbone 常识」，标注为待实现。

#### 4.5.2 核心流程

一个典型的 Wishbone master 读 slave 的单拍流程（简化）：

```
master:  CYC_O=1, STB_O=1, ADR_O=地址, WE_O=0(读)
slave :  准备数据 → DAT_O=数据, ACK_O=1
master:  看到 ACK_O=1 → 锁存 DAT_I → CYC_O=0 结束
```

把 PoC 核包成 Wishbone slave 的通用思路：

1. 在 slave 侧维护一个 Wishbone 接口（`CYC_I/STB_I/ADR_I/DAT_I/WE_I` 入，`ACK_O/DAT_O` 出）。
2. 内部把 Wishbone 的读写翻译成对 PoC 核的 PoC.Stream 或 FIFO 操作（例如 `put`/`got`）。
3. 因为 Wishbone 是「地址 + 单拍应答」，而 PoC.Stream 是「帧 + 握手」，适配器需要处理**两种协议的时序差**——通常用一个状态机把 Wishbone 的单次访问展开成对 PoC 核的若干拍握手。

#### 4.5.3 源码精读

`bus/wb/README.md` 只列出规划，没有实现：

[src/bus/wb/README.md:14-18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/wb/README.md#L14-L18) — 列出 `wb_fifo_adapter` / `wb_ocram` / `wb_uart_wrapper` 三个规划实体。

`PoC.bus` 的总 README 同样把 wb 标为「Modules for the WISHBONE bus」：

[src/bus/README.md:19-26](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/README.md#L19-L26) — `bus_Arbiter` 是 `PoC.bus` 下唯一已实现的顶层实体。

> ⚠️ 与之相关的 `bus.pkg.vhdl`（README 称其「holds all component declarations」）在当前快照也**不存在**——`bus_Arbiter.files` 把它注释掉了，`bus_Arbiter.vhdl` 直接 `use PoC.utils.all`，不走 `bus.pkg`。这与 u3-l1 讲的「命名空间根包模式」并不矛盾，只是 `bus` 这个命名空间目前只有一个实体，作者还没把根包补上。

#### 4.5.4 代码实践

**实践目标**：基于 README 的规划，为 `wb_ocram` 画一个 Wishbone slave 接口草案。

**操作步骤**（设计草案型实践，不写源码）：

1. 阅读 [src/bus/wb/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/wb/README.md) 与 u3-l3 的 `ocram_sp` 接口。
2. 在纸上为 `wb_ocram` 设计端口：Wishbone slave 侧（`CLK_I/RST_I/CYC_I/STB_I/ADR_I/DAT_I/WE_I/ACK_O/DAT_O`），内部实例化一个 `ocram_sp`（端口 `Clock/Reset/...`）。
3. 写出最简的地址映射：`ADR_I` 直接连 `ocram_sp` 的地址口，`WE_I` 决定读写，`ACK_O` 在 `CYC_I and STB_I` 时单拍回 1。

**需要观察的现象**：

- 一次 Wishbone 写：`CYC_I=1, STB_I=1, WE_I=1, ADR_I=a, DAT_I=d` → 当拍 `ocram_sp` 写入地址 a，`ACK_O=1`。
- 一次 Wishbone 读：`WE_I=0` → 当拍 `DAT_O` 给出地址 a 的内容，`ACK_O=1`。

**预期结果**：因为 `ocram_sp` 是单周期访问，`wb_ocram` 应能做成**单拍应答**的 slave（每拍 `CYC_I&STB_I` 成立即回 `ACK_O`），是最简单的适配情形。复杂的（如把 PoC.Stream 帧接口包成 Wishbone）则需要多拍状态机。

> 运行结果：**待本地验证 / 待实现**（源码尚不存在；本实践仅为接口设计练习）。

#### 4.5.5 小练习与答案

**练习 1**：Wishbone 的 `ACK_O` 与 PoC.Stream 的 `Ack` 在语义上有何异同？

**参考答案**：都是「目的端告诉源端：这次访问/搬运成交」。区别在于粒度：Wishbone 的 `ACK_O` 是对一次总线访问（含地址）的应答；PoC.Stream 的 `Ack` 是对一个数据字的应答，且 PoC.Stream 没有地址，靠 SOF/EOF 划帧。

**练习 2**：为什么把 PoC.Stream 包成 Wishbone slave 比 `wb_ocram` 难？

**参考答案**：`ocram_sp` 是单周期随机访问，地址直接对应，可以单拍应答。PoC.Stream 是「按字握手 + 按帧成组」的流式接口，一次 Wishbone 访问可能要展开成对 PoC.Stream 的若干拍 `Valid/Ack` 握手才能拿到一个字（尤其涉及帧首/帧尾时），因此需要状态机处理协议时序差。

**练习 3**：当前快照下，如果你想在 Wishbone SoC 里用一个 PoC 核，最务实的做法是什么？

**参考答案**：由于 `wb_*` 适配器尚未实现，最务实的是参考 u5-l6（扩展 PoC）的方法**自己写一个最小 Wishbone wrapper**：把目标 PoC 核的 PoC.Stream/FIFO 接口包成 Wishbone slave。本节的接口设计练习就是为此做准备。

---

## 5. 综合实践

设计一个**「两路数据汇流到一条弹性缓冲」**的小系统，把本讲全部主线串起来：

**目标拓扑**：

```
stream_Source(端口0) ─┐
                       ├─→ stream_Mux ─→ stream_Buffer ─→ 手写 sink
stream_Source(端口1) ─┘
```

**任务清单**：

1. **数据源**：用两个 `stream_Source` 实例（或一个 `stream_Source` + 一个手写源进程），分别构造一帧 8 位数据（如端口 0 发 `[0x01,0x02,EOF]`，端口 1 发 `[0xAA,0xBB,EOF]`）。
2. **仲裁**：接入 `stream_Mux`（`PORTS=2, DATA_BITS=8`）。先在纸上预测哪一帧先出（提示：看 [stream_Mux.vhdl:95](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/bus/stream/stream_Mux.vhdl#L95) 的指针初值，指向最高端口）。
3. **缓冲**：在 `stream_Mux` 输出与 sink 之间插入 `stream_Buffer`，并把 sink 的 `Out_Ack` 改成「有时故意不回」以制造背压，观察 `stream_Buffer` 是否把整帧缓存住、不丢字。
4. **回环验证**：手写 sink 收下所有字，累计校验：收到的字序是否与预测一致、两帧是否各自完整（SOF…EOF 不交错）。
5. **时序说明**：在报告中用 4.2.2 的时序图，标注出一次「sink 没回 Ack → `stream_Source` 停在原字 → `stream_Buffer` 暂停前进」的背压链路。

**验收要点**：

- 能解释清楚「仲裁（按帧选端口）→ 缓冲（按帧 commit）→ 握手（`Valid&Ack` 成交）」三层如何协作。
- 能说出当前快照下哪些组件可用、哪些待实现（`stream_Sink`、`wb_*`、`bus.pkg`）。
- 时序图与实际仿真波形一致（**待本地验证**）。

> 这个综合实践覆盖了本讲全部三个最小模块：总线仲裁（`bus_Arbiter` 思想在 `stream_Mux` 内复用）、流式组件（`Mux`/`Buffer`）、以及为后续 Wishbone 接入打下的协议时序基础。

## 6. 本讲小结

- `PoC.bus` 命名空间提供总线相关积木；当前快照下已实现的顶层实体只有 `bus_Arbiter`，它是纯控制模块，用「独热指针 + `(not X)+1` 位运算」实现 Round-Robin 仲裁，`OUTPUT_REG` 控制授权是否寄存一拍。
- PoC.Stream 是一套类 AXI-Stream 的流式协议：用 `Valid`/`Ack` 握手（`Ack` 等价于 ready）、`SOF`/`EOF` 成帧、`Meta` 做边带；成交条件是「`Valid & Ack` 同拍有效」。
- 四个可综合流式组件中，`stream_Mux`（N→1）和 `stream_DeMux`（1→N）**复用了与 `bus_Arbiter` 同源的位运算仲裁**，并都「按帧」决策（在 SOF 锁定通道、EOF 释放）；`stream_Buffer` 用 `fifo_cc_got` + `fifo_cc_got_tempgot` 实现「按帧 commit/rollback」的弹性缓冲；`stream_Mirror` 用 `fifo_glue` 做一拍解耦的广播。
- `stream_Source` 是**仿真专用** BFM，按帧组驱动 PoC.Stream 源端，下降沿采样 `Ack`；README 列出的 `stream_Sink` 在当前快照**未实现**，回环验证需手写 sink 进程。
- `PoC.bus.wb` 规划了三个 Wishbone 适配器（`wb_fifo_adapter`/`wb_ocram`/`wb_uart_wrapper`），但当前快照**只有 README、源码待实现**；`bus.pkg.vhdl` 同样缺失，`bus_Arbiter` 直接用 `PoC.utils`。
- 贯穿全讲的「设计一致性」：仲裁、握手、FIFO、按帧提交这些思想在 PoC 的不同组件里反复以同一种位运算和 generic（`OUTPUT_REG`/`log2ceilnz`/`onehot2bin`）出现——理解了 4.1 的仲裁，就读懂了 4.3 一半的代码。

## 7. 下一步学习建议

- **横向扩展到网络栈**：本讲的流式握手与成帧是下一讲 **u5-l5 网络协议栈（net 命名空间）** 的直接基础——MAC/ARP/IPv4/UDP 的 Wrapper 都在用 PoC.Stream 风格的接口搬运帧。建议带着「`Valid/Ack/SOF/EOF` 在协议层之间如何传递」的问题去读 `mac_Wrapper`。
- **纵向深入缓存与总线**：如果想看更复杂的多 master 仲裁与总线接入，结合 **u5-l3 cache 子系统**（cache_cpu 的 FSM 访问主存）一起看，体会仲裁器在真实子系统中的位置。
- **动手扩展**：参考 **u5-l6 扩展 PoC**，尝试实现一个真正的 `wb_ocram`（见 4.5.4 的接口草案），把你第一份 Wishbone wrapper 贡献回项目——这是把本讲「待实现」变成「已实现」的最直接路径。
- **源码继续阅读**：若想验证本讲的时序推断，可在 `tb/` 目录寻找是否已有 stream 相关测试台（`git ls-files tb/bus/`）；若无，则按 u4-l2 的骨架自建。
