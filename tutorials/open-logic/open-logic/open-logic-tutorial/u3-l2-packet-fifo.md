# 包 FIFO（olo_base_fifo_packet）

## 1. 本讲目标

本讲在「同步 FIFO」（[u2-l4](u2-l4-sync-fifo.md)）的基础上，进入**包（packet）级**的数据缓冲。学完后你应当能够：

- 说清「包 FIFO」与普通 FIFO 的本质区别，以及**存储转发（store-and-forward）**带来的好处与限制。
- 解释 `olo_base_fifo_packet` 用「大 RAM + 小 FIFO」界定包边界、并给出 `Out_Size`/`Out_Last` 的原理。
- 掌握**写侧丢包**（`In_Drop`/`In_IsDropped`，含超长包自动丢弃）和**读侧跳过/重复**（`Out_Next`/`Out_Repeat`）两条机制。
- 能在 AXI-S 包流中正确实例化该实体，并运行它的 VUnit 测试台验证行为。

> 本讲只讲 `olo_base_fifo_packet` 一个实体。同步 FIFO 的指针/计数器基础请先复习 [u2-l4](u2-l4-sync-fifo.md)，RAM 的 RBW/WBR 行为请复习 [u2-l3](u2-l3-ram-implementations.md)。

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲清楚。

**字（word / beat）与包（packet）。** AXI4-Stream（AXI-S）接口里，数据是按「字」一拍一拍流动的，每拍由 `Valid`/`Ready` 握手传递。一个**包**是若干连续的字组成的逻辑单元，用 `Last`（即 AXI-S 的 `TLAST`）标记包的**最后一个字**。也就是说：从一个 `Last` 之后到下一个 `Last`（含）之间的所有字，构成一个完整的包。

```text
字0  字1  字2(Last) | 字0  字1(Last) | 字0 字1 字2 字3(Last)
└──── 包 A ────────┘   └── 包 B ─────┘  └──────── 包 C ────────┘
```

**存储转发 vs 直通（cut-through）。**
- **存储转发**：先把整个包**完整地**写进缓冲，再允许从输出读出。好处是「包被压缩」——哪怕写得很慢，一旦写完就能以满速率一口气读出；坏处是缓冲必须放得下整个包。
- **直通**：还没等整包到齐就开始往外送，可以降低延迟，但实现复杂。本实体**只做存储转发**，因此**大于 `Depth_g` 的包无法处理、会被自动丢弃**。

**普通 FIFO vs 包 FIFO。** 普通 FIFO 只认「字」，进出都是逐字的，不关心包边界。包 FIFO 在此之上额外记住「每个包从哪到哪」，从而支持**整包级别**的操作：丢一整包、跳过一整包、重复读一整包、报告当前包大小、报告 FIFO 里还有几个包。

**AXI-S 握手与反压。** 数据仅在 `Valid=1 且 Ready=1` 的时钟沿完成一次传递（一次 transaction / 一次 transaction）；下游可以用 `Ready=0` 施加反压（back-pressure）。本讲假设你已熟悉这套握手。

**关键术语速查**

| 术语 | 含义 |
| :--- | :--- |
| beat / word | 一个时钟周期传递的数据字 |
| packet | 由若干 word 组成、以 `Last` 收尾的逻辑单元 |
| store-and-forward | 整包写入完成后再输出 |
| 反压（back-pressure） | 下游用 `Ready=0` 暂停上游发送 |
| DropLatch | 写侧「本包已标记丢弃」的锁存位 |
| FeatureSet | 功能子集（FULL / DROP_SKIP_ONLY / DROP_ONLY） |

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/base/vhdl/olo_base_fifo_packet.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd) | 本讲的唯一核心实体，实现同步包 FIFO |
| [doc/base/olo_base_fifo_packet.md](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md) | 官方文档，含波形图与功能集说明 |
| [test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd) | VUnit 测试台，覆盖丢包/跳过/重复等场景 |
| [sim/test_configs/olo_base.py](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py) | 用 `named_config` 把该测试台按 FeatureSet/Optimization 参数化成多组用例 |

实体内部还复用了两个已学过的实体：存负载的 [olo_base_ram_sdp](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_ram_sdp.vhd)（[u2-l3](u2-l3-ram-implementations.md)），以及存「包尾地址」的小 FIFO [olo_base_fifo_sync](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_sync.vhd)（[u2-l4](u2-l4-sync-fifo.md)）。

## 4. 核心概念与源码讲解

### 4.1 存储转发机制：大 RAM + 包尾地址小 FIFO

#### 4.1.1 概念说明

`olo_base_fifo_packet` 是一个**同步**包 FIFO（读写同一时钟）。它在文件头描述里直接点明了自己的定位：

> 这是一个同步包 FIFO。与普通 FIFO 不同，它允许丢包、重复包，并能检测 FIFO 里有多少个包。FIFO 工作在**存储转发**模式。FIFO 假定所有包都能放进 FIFO；为处理比 FIFO 还大的包所需的**直通**操作**未实现**。

见 [src/base/vhdl/olo_base_fifo_packet.vhd:9-14](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L9-L14)（实体头注释，说明存储转发与不支持直通）。

存储转发的核心矛盾是：**负载是连续的字流，但「包」是有边界的逻辑单元**。于是实体用了「两块存储」的分工：

- **大 RAM**：存所有包的**负载字**，按地址顺序连续写、连续读，和普通 FIFO 一样。
- **小 FIFO**：每存完一个包，把它的**尾地址（end address）**推进去一个独立的小 FIFO。读侧每读一个包，就从这个小 FIFO 取出对应的尾地址，从而知道这个包到哪里结束、共多大。

文档把这套结构总结为：*The FIFO contains a large RAM for packet data and a small olo_base_fifo_sync for storing the sizes of individual packets.* 见 [doc/base/olo_base_fifo_packet.md:52-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L52-L53)。

#### 4.1.2 核心流程

写侧（每个 `Valid&Ready` 的字）：

1. 把 `In_Data` 写入大 RAM 当前 `WrAddr`，`WrAddr` 前进一格。
2. 若是包内非末字：继续等下一个字。
3. 若是末字（`In_Last=1`）且**未被丢弃**：把「包尾地址」推进小 FIFO，标记本包可被读侧看到。
4. 若是末字且**被丢弃**：把 `WrAddr` **回卷**到包首，相当于这个包从没写过；不推进小 FIFO。

读侧用一个三态有限状态机（FSM）：

```text
          取下一个包尾地址            读到倒数第二字
 Fetch_s ──────────────► Data_s ──────────────────► Last_s
   ▲                       │  (Out_Next: 提前收尾)      │
   │                       └─────────────►  (Out_Last)  │
   └────────────────────────────────────────────────────┘
                   (Out_Repeat: 回到本包首重读)
```

- `Fetch_s`：空闲，从小 FIFO 取下一个包的尾地址，算出包大小。
- `Data_s`：逐字流出负载，直到接近包尾。
- `Last_s`：拉高 `Out_Last` 送出最后一字，再回 `Fetch_s` 准备下一包。

#### 4.1.3 源码精读

**(a) 泛型与端口总览。** 见 [src/base/vhdl/olo_base_fifo_packet.vhd:38-72](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L38-L72)。几个关键泛型：

- `Depth_g`：大 RAM 的字数，**必须是 2 的幂**（下文断言强制）。
- `MaxPacketSize_g`：单个包最大字数，`-1` 表示不限（但仍受 `Depth_g` 兜底）。
- `FeatureSet_g`：功能子集 `FULL` / `DROP_SKIP_ONLY` / `DROP_ONLY`（见 4.4）。
- `Optimization_g`：`SPEED`（每包读侧多 1 个 stall 周期）或 `THROUGHPUT`（背靠背包无 stall，但仅限非 FULL）。
- `MaxPackets_g`：小 FIFO 容量相关，限制 FIFO 内最多能存多少包。

端口里除了标准 AXI-S 数据/握手，还有包级控制信号 `In_Drop`/`In_IsDropped`/`Out_Next`/`Out_Repeat`/`Out_Size`，以及状态 `PacketLevel`/`FreeWords`。注意**所有可选输入端口都有默认值**（如 `In_Last : in std_logic := '1'`），不用某功能时可以直接悬空——这是 Open Logic「易用」哲学的体现。

**(b) 两进程法 + record。** 实体沿用全库通用的「两进程法」（复习 [u1-l5](u1-l5-conventions-and-anatomy.md)、[u2-l2](u2-l2-pipeline-stage-handshake.md)）：所有寄存器收进一个 record，组合进程 `p_comb` 只算下一拍 `r_next`，时序进程 `p_seq` 只打拍+复位。record 里同时含写侧（`WrAddr`/`WrPacketStart`/`WrSize`/`WrPacketActive`/`DropLatch`/`Full`）与读侧（`RdAddr`/`RdPacketStart`/`RdPacketEnd`/`RdValid`/`RdFsm`/`RdRepeat`/`RdSize`/`NextLatch`）寄存器，见 [olo_base_fifo_packet.vhd:95-114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L95-L114)。读 FSM 的三个状态定义在 [olo_base_fifo_packet.vhd:93](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L93)：

```vhdl
type RdFsm_t is (Fetch_s, Data_s, Last_s);
```

**(c) 地址空间与满/空区分。** 指针比 RAM 实际地址多一位最高位，用来区分「满」与「空」。注释 [olo_base_fifo_packet.vhd:116-117](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L116-L117) 与子类型定义 [olo_base_fifo_packet.vhd:89-90](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L89-L90) 说明：`Addr_c` 比 `AddrApp_c` 多一位。复位时写指针初始化为 `Depth_g`、读指针初始化为 `0`（见 [olo_base_fifo_packet.vhd:430-437](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L430-L437)），再配合「写地址最高位取反」的技巧，让「读写地址相等」明确表示**空**而非满：

```vhdl
WrAddrStdlv(AddrApp_c)        <= std_logic_vector(r.WrAddr(AddrApp_c));
WrAddrStdlv(WrAddrStdlv'high) <= not r.WrAddr(r.WrAddr'high);  -- 最高位取反
```

见 [olo_base_fifo_packet.vhd:451-453](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L451-L453)。这个取反后的 `WrAddrStdlv` 就是写进小 FIFO 的「包尾地址」。满的判定则在写侧直接比较原始指针：`r.WrAddr = r.RdPacketStart-1` 时置 `Full`，见 [olo_base_fifo_packet.vhd:196-198](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L196-L198)。

**(d) 大 RAM 与小 FIFO 的例化。** 负载 RAM 例化的是 `olo_base_ram_sdp`，把多出来的最高位地址剥掉后用作真实 RAM 地址，见 [olo_base_fifo_packet.vhd:481-495](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L481-L495)：

```vhdl
i_ram : entity work.olo_base_ram_sdp
    generic map ( Depth_g => Depth_g, Width_g => RamWidth_c, ... )
    port map ( Wr_Addr => WrAddrStdlv(AddrApp_c), Wr_Ena => RamWrEna,
               Wr_Data => RamInData, Rd_Addr => RamRdAddr(AddrApp_c), Rd_Data => RamOutData );
```

包尾地址的小 FIFO 例化的是 `olo_base_fifo_sync`，宽度为 `log2ceil(Depth_g)+1`、深度与 `MaxPackets_g` 相关，见 [olo_base_fifo_packet.vhd:500-522](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L500-L522)。注意写侧把 `WrAddrStdlv`（尾地址）经 `FifoInValid` 推入，读侧经 `RdPacketEnd` 取出。

**(e) DROP_ONLY 的简化。** 当 `FeatureSet_g=DROP_ONLY` 时，小 FIFO 被一个单寄存器替代——只锁存「最近一个完整写入包」的尾地址，见 [olo_base_fifo_packet.vhd:525-551](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L525-L551)。这是有意的资源优化：当包大小变化很大、或目标器件没有分布式 RAM（小 FIFO 会吃掉一整块 BRAM 或大量 FF）时，`DROP_ONLY` 省掉这块开销，代价是失去 `Out_Size`/`Out_Next`/`Out_Repeat`。文档对此有详细说明，见 [doc/base/olo_base_fifo_packet.md:164-179](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L164-L179)。

#### 4.1.4 代码实践

**目标**：用现成的 VUnit 测试台直观感受「存储转发」——即写得很慢、读得很快时，整包仍能被一口气读出。

**操作步骤**：

1. 列出该实体所有测试用例，找到想要的配置全名：

   ```bash
   cd sim
   python3 run.py --ghdl -l "*fifo_packet_tb*"
   ```

   > `-l` / `--list` 是 VUnit 列测试的选项；位置参数是名字过滤模式。配置注册见 [sim/test_configs/olo_base.py:241-254](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L241-L254)，会按 `FULL/DROP_ONLY/DROP_SKIP_ONLY` × `SPEED/THROUGHPUT` 展开多组。

2. 跑一个明确体现「写慢读快」的用例 `LimitedInputRate`（写侧每拍间隔 10 个时钟），并在仿真器里打开波形：

   ```bash
   python3 run.py --ghdl "olo_tb.olo_base_fifo_packet_tb*LimitedInputRate*"
   ```

3. 在波形里对照 `In_Valid/In_Ready`（写侧稀疏）与 `Out_Valid/Out_Ready`（读侧连续）。

**需要观察的现象**：写侧因为 `InDelay_v := 10*ClockPeriod_c` 而很稀疏（见测试台 [olo_base_fifo_packet_tb.vhd:262-268](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L262-L268)），但读侧一旦开始输出某个包，就是连续无停顿的，直到 `Out_Last` 拉起。

**预期结果**：读侧波形呈现「整包突发」，中间没有因写侧慢而产生的间隙——这就是存储转发带来的「包压缩」。

**运行结果**：待本地验证（取决于是否已安装 GHDL 与 VUnit 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Depth_g` 必须是 2 的幂？请指出源码中强制该约束的位置。

**参考答案**：因为读写指针在一个 \(2\cdot\text{Depth}_g\) 的环形空间里回绕，只有 2 的幂才能保证回绕点地址干净对齐。断言见 [olo_base_fifo_packet.vhd:137-139](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L137-L139)：`assert log2(Depth_g) = log2ceil(Depth_g) ... severity error`。

**练习 2**：小 FIFO 的深度为什么和 `MaxPackets_g-1` 有关，而不是 `MaxPackets_g`？

**参考答案**：因为读侧每取一个包，就会立刻把对应的尾地址从小 FIFO 弹出（`FifoOutRdy`），所以「正在被读出的那一个包」不占用小 FIFO；文档原话见 [doc/base/olo_base_fifo_packet.md:156-160](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L156-L160)，源码深度计算见 [olo_base_fifo_packet.vhd:506](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L506)。

---

### 4.2 包边界界定：Last 信号与 Out_Size

#### 4.2.1 概念说明

> 关于本讲的「Last/Be」：在通用 AXI-S 里，`TKEEP`/`TSTRB`（字节使能，byte enable）有时用来描述一拍数据里哪些字节有效。但 **`olo_base_fifo_packet` 并不使用任何字节使能来界定包边界**——它的包边界**完全且仅由 `Last` 信号**决定。本节我们只讲 Last（以及由它派生出的 `Out_Size`），不会出现也无需关心 Be。

包边界有两层含义：

- **输入侧**：写侧用 `In_Last` 告诉 FIFO「当前字是这个包的最后一个」。FIFO 据此知道一个包写完了。
- **输出侧**：FIFO 用 `Out_Last` 告诉下游「当前字是输出包的最后一个」，并额外用 `Out_Size` 给出本包的总字数。

#### 4.2.2 核心流程

写侧到达 `In_Last` 时分两种情况：

```text
末字到来 (In_Last=1)
   ├── 本包被丢弃 (DropLatch=1) → WrAddr 回卷到包首，不入小 FIFO
   └── 本包正常     → WrPacketStart := WrAddr+1，把尾地址推入小 FIFO
```

读侧从小 FIFO 拿到尾地址 `RdPacketEnd` 后：

- 包大小 \(\text{RdSize} = \text{RdPacketEnd} - \text{RdAddr} + 1\)，通过 `Out_Size` 输出。
- 读到 `RdAddr = RdPacketEnd-1` 的那一字时进入 `Last_s`，拉高 `Out_Last`。

#### 4.2.3 源码精读

**(a) 写侧末字处理。** 见 [olo_base_fifo_packet.vhd:220-234](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L220-L234)：

```vhdl
-- Handle end of packet
if In_Last = '1' then
    -- Packet dropped
    if InDrop_v = '1' then
        v.WrAddr := r.WrPacketStart;          -- 回卷：丢弃已写部分
    -- Packet stored
    else
        v.WrPacketStart := r.WrAddr + 1;       -- 更新下一个包的起始
        FifoInValid     <= '1';                -- 把尾地址推入小 FIFO
    end if;
    v.DropLatch      := '0';
    v.WrSize         := to_unsigned(1, r.WrSize'length);
    v.WrPacketActive := '0';
end if;
```

注意 `FifoInValid` 仅在「正常存完一个包」时才拉一拍，这就是「尾地址进小 FIFO」的触发点（小 FIFO 写侧连的是 `WrAddrStdlv`，见 [olo_base_fifo_packet.vhd:514-515](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L514-L515)）。

**(b) 读侧计算包大小与 Out_Size。** `Fetch_s` 里算出 `RdSize`，见 [olo_base_fifo_packet.vhd:289-303](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L289-L303)：

```vhdl
v.RdPacketEnd := unsigned(RdPacketEnd);
v.RdValid     := '1';
if not compareNoCase(FeatureSet_g, "drop_only") then
    v.RdSize := unsigned(RdPacketEnd) - r.RdAddr + 1;   -- 包大小
end if;
```

`Out_Size` 直接由 `RdSize` 驱动（`DROP_ONLY` 时输出 `X`），见 [olo_base_fifo_packet.vhd:384-389](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L384-L389)。

**(c) DROP_ONLY 如何在没有小 FIFO 时还能给出 Last。** `DROP_ONLY` 把 `In_Last` 这一比特**和负载拼在一起存进大 RAM**（RAM 宽度 `Width_g+1`），读侧再把这一比特还原成 `Out_Last`，见 [olo_base_fifo_packet.vhd:458](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L458) 与 [olo_base_fifo_packet.vhd:466-478](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L466-L478)。这是一种很巧的取舍：既然没有小 FIFO 来记尾地址，就把 Last 比特随数据一起存下来。

#### 4.2.4 代码实践

**目标**：验证 `Out_Size` 与 `Out_Last` 的正确性。

**操作步骤**：

1. 跑一个多字包用例 `TwoPackets`（一个 3 字包 + 一个 4 字包），见测试台 [olo_base_fifo_packet_tb.vhd:257-260](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L257-L260)：

   ```bash
   cd sim && python3 run.py --ghdl "olo_tb.olo_base_fifo_packet_tb*TwoPackets*"
   ```

2. 测试台用 `checkPacket` 校验每包：在末字检查 `tlast='1'`、`tuser`(即 `Out_Size`) 等于包字数，见 [olo_base_fifo_packet_tb.vhd:154-172](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L154-L172)。

**需要观察的现象**：`Out_Last` 恰好在每个包的最后一字拉高；该拍 `Out_Size` 分别为 3 和 4。

**预期结果**：测试通过；波形上每个包只有最后一字带 `Out_Last`，且 `Out_Size` 与包字数一致。

**运行结果**：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：若 `FeatureSet_g=DROP_ONLY`，`Out_Size` 的值是什么？为什么？

**参考答案**：输出全 `X`（无效）。因为 `DROP_ONLY` 不维护每个包的尾地址（用单寄存器替代了小 FIFO），无法算出包大小；见 [olo_base_fifo_packet.vhd:384-388](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L384-L388)。

**练习 2**：`DROP_ONLY` 模式下没有小 FIFO，`Out_Last` 是怎么产生的？

**参考答案**：写侧把 `In_Last` 比特并入 RAM 数据一起存（RAM 宽 `Width_g+1`），读侧从 RAM 读出后把该比特单独还原为 `Out_Last`，见 [olo_base_fifo_packet.vhd:466-478](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L466-L478)。

---

### 4.3 写侧丢包：In_Drop / In_IsDropped

#### 4.3.1 概念说明

很多场景下，**是否丢弃一个包要等到包快结束时才知道**——例如包尾的 CRC 校验错误。如果非要等校验完再开始写 FIFO，就需要在外面额外缓存整个包。`olo_base_fifo_packet` 允许你**一边写一边判断**：发现要丢，就随时在包内任意一拍（从首字到末字）拉高 `In_Drop`，整个包（含已经写进 RAM 的部分）都会被丢弃，写指针自动回卷。

官方给出的典型用例就是 CRC：*A CRC error can only be detected at the end of the packet. User logic can still write the packet into the FIFO directly and just asserts In_Drop if a CRC error is detected at the end.* 见 [doc/base/olo_base_fifo_packet.md:24-29](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L24-L29)。

除手动 `In_Drop` 外，还有两种**自动丢包**：包长超过 `Depth_g`（存储转发放不下）、或超过 `MaxPacketSize_g`（用户设的上限）。

#### 4.3.2 核心流程

写侧用 `DropLatch` 这个锁存位「记住本包已经被判死刑」：

```text
包内任一拍 In_Drop=1 ──► DropLatch := 1 ──► (持续到 In_Last)
                                         │
   期间 In_Ready 恒为 1 (即使 Full) ─────► 把包剩余字"抽干"，避免堵塞
                                         │
   到 In_Last:  因 DropLatch=1 ──► WrAddr 回卷到包首，不推小 FIFO
```

关键点：**一旦 `DropLatch=1`，`In_Ready` 强制为 1**，让 FIFO 赶紧把剩下的字吃掉（反正要丢），否则一个被判死刑的大包会卡住后续正常包。

#### 4.3.3 源码精读

**(a) DropLatch 的置位。** 「包间」（即使没有有效 transaction）也能检测到 `In_Drop`，见 [olo_base_fifo_packet.vhd:187-189](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L187-L189)；在有效 transaction 内也会再次确认，见 [olo_base_fifo_packet.vhd:207-211](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L207-L211)：

```vhdl
-- Handle packet drop between samples
if In_Drop = '1' and (r.WrPacketActive = '1' or In_Valid = '1') then
    v.DropLatch := '1';
end if;
```

**(b) In_Ready 在丢弃期间强制拉高（抽干包）。** 见 [olo_base_fifo_packet.vhd:176](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L176)：

```vhdl
In_Ready_v := ((not r.Full) and FifoInReady) or r.DropLatch;  -- DropLatch=1 时必为 1
```

**(c) 超长包自动丢弃。** 当累计字数 `WrSize` 达到上限 `MaxPktSize_c`，置 `DropLatch`，见 [olo_base_fifo_packet.vhd:213-218](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L213-L218)。`MaxPktSize_c` 默认等于 `Depth_g`（`MaxPacketSize_g=-1` 时），定义见 [olo_base_fifo_packet.vhd:84](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L84)。这就是「大于 Depth_g 的包自动丢」的实现。

**(d) 写指针回卷与不推小 FIFO。** 如 4.2.3(a) 所示，`In_Last` 且 `InDrop_v='1'` 时 `v.WrAddr := r.WrPacketStart`（回卷），且**不**执行 `FifoInValid <= '1'`（不推尾地址），于是读侧永远看不到这个包。同时为了避免回卷后的指针被满检测误判堵塞，额外把 `Full` 清零，见 [olo_base_fifo_packet.vhd:242-245](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L242-L245)。

**(e) In_IsDropped 状态回馈。** `In_IsDropped <= InDrop_v` 把「本包是否会被丢」实时告诉用户，免去用户自己记 `In_Drop` 历史，见 [olo_base_fifo_packet.vhd:248](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L248)。文档对其时序的说明见 [doc/base/olo_base_fifo_packet.md:186-190](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L186-L190)。

#### 4.3.4 代码实践（本讲主实践）

**目标**：向 `olo_base_fifo_packet` 写入若干包后，在写侧对其中一个包触发 `In_Drop`，仿真验证**读侧只输出完整包、被丢弃的包不出现**。

**操作步骤**：

1. 直接复用现成的 `DropPacketMiddle` 用例。它在 3 个字包的第 0/1/2 字分别触发一次丢包，并在前后夹正常包，验证「被丢的包不出现、正常包按原值出现」：

   ```bash
   cd sim
   python3 run.py --ghdl "olo_tb.olo_base_fifo_packet_tb*DropPacketMiddle*"
   ```

   对应测试代码见 [olo_base_fifo_packet_tb.vhd:351-361](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L351-L361)：

   ```vhdl
   if run("DropPacketMiddle") then
       for dropWord in 0 to 2 loop
           testPacket(net, 3, 1);                        -- 正常包(值 1,2,3)
           pushPacket(net, 3, 16, dropAt => dropWord);   -- 包(16,17,18) 丢在第 dropWord 字
           testPacket(net, 3, 32);                       -- 正常包(32,33,34)
           wait for CaseDelay_c;
       end loop;
   end if;
   ```

   `pushPacket` 用 `tuser(0)` 承载 `In_Drop`（见 [olo_base_fifo_packet_tb.vhd:109-118](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L109-L118)），VC 例化把它连到 DUT 的 `In_Drop`（见 [olo_base_fifo_packet_tb.vhd:864-875](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L864-L875)）。同时 VC 还顺带检查 `In_IsDropped` 在丢包期间拉高（见 [olo_base_fifo_packet_tb.vhd:97-103](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L97-L103)）。

2. 想看波形时，加仿真器选项打开 GUI（GHDL 可用 `--ghdl.gtkwave` 导出 FST 后查看；具体语法以本机 VUnit 版本为准）。

**需要观察的现象**：
- 读侧依次出现 `1,2,3`（包 A）→ 直接跳到 `32,33,34`（包 C），中间的 `16,17,18`（包 B）**完全不出现**。
- 在写包 B 期间，`In_IsDropped` 在 `In_Drop` 置位后的 transaction 起开始拉高，直到 `In_Last`。
- `In_Ready` 在丢包期间保持高，把 B 的剩余字抽干。

**预期结果**：三组 dropWord（0/1/2）全部通过；`checkPacket` 不会在任何时刻收到 `16/17/18` 的值。

**运行结果**：待本地验证。

> 进阶：想亲手构造场景，可在测试台的 `Random` 用例里观察——它以 10% 概率随机丢包、10% 概率随机跳过/重复，见 [olo_base_fifo_packet_tb.vhd:751-802](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L751-L802)。这是「丢包 + 跳过 + 重复」联合的约束随机验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `In_Drop` 一旦在包内被置位，`In_Ready` 就要在剩余字里保持高？

**参考答案**：被判死刑的包需要尽快从输入「抽干」，否则它会占着写通路、堵住后面正常的包；强制 `In_Ready=1` 让剩余字被无声丢弃。见 [olo_base_fifo_packet.vhd:176](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L176)。

**练习 2**：一个长度为 `Depth_g+1` 的包会发生什么？依据是哪段代码？

**参考答案**：写到第 `Depth_g` 个字时 `WrSize` 触顶，置 `DropLatch`，整包被丢弃。依据见 [olo_base_fifo_packet.vhd:213-218](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L213-L218)（`MaxPktSize_c` 默认 = `Depth_g`）。测试台 `OversizedPacket-Middle` 正是验证这一点，见 [olo_base_fifo_packet_tb.vhd:726-730](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L726-L730)。

---

### 4.4 读侧跳过与重复：Out_Next / Out_Repeat（含 FeatureSet）

#### 4.4.1 概念说明

读侧也提供整包级操作：

- **跳过（`Out_Next`）**：读包过程中，发现这个包不感兴趣（比如包头里的类型不对），随时拉高 `Out_Next`，FIFO 立刻在当前字提前给出 `Out_Last` 收尾，并跳到下一个包。剩余未读的字被丢弃，但**包边界（`Out_Last`）不会被省略**，完全符合 AXI-S 协议。典型用例见 [doc/base/olo_base_fifo_packet.md:30-33](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L30-L33)。
- **重复（`Out_Repeat`）**：读完一个包后还想再读一遍同一个包（比如无线发送发生冲突要重传），拉高 `Out_Repeat`，FIFO 把读指针拨回包首，**重读同一包**（数据并未从 FIFO 删除）。典型用例见 [doc/base/olo_base_fifo_packet.md:34-38](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L34-L38)。两者还能同时拉高，实现「立刻丢弃当前剩余字 + 重复本包」。

**功能子集（FeatureSet_g）。** 这些读侧功能不是免费的，于是实体提供三档（见 [doc/base/olo_base_fifo_packet.md:58-70](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L58-L70)）：

| FeatureSet_g | 写侧丢包 | Out_Next(跳过) | Out_Repeat(重复) | Out_Size |
| :--- | :---: | :---: | :---: | :---: |
| FULL | ✓ | ✓ | ✓ | ✓ |
| DROP_SKIP_ONLY | ✓ | ✓ | ✗ | ✓ |
| DROP_ONLY | ✓ | ✗ | ✗ | ✗（输出 X） |

档位越低越省资源（`DROP_ONLY` 连小 FIFO 都省了）。此外 `Optimization_g` 控制 `SPEED`（每包读侧多 1 个 stall 周期，所有 FeatureSet 支持）与 `THROUGHPUT`（背靠背包零 stall，仅非 FULL 支持），见 [doc/base/olo_base_fifo_packet.md:72-83](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L72-L83)。

#### 4.4.2 核心流程

跳过（`Out_Next`，在 `Data_s` 内）：

```text
Data_s 中 Out_Next=1 ──► 当前字立即拉 Out_Last
                         RdAddr := RdPacketEnd+1  (跳到下一包首)
                         回到 Fetch_s
```

重复（`Out_Repeat`，经 `RdRepeat` 锁存，在 `Fetch_s` 处理）：

```text
Out_Repeat=1 ──► RdRepeat := 1 (锁存)
读完本包回 Fetch_s ──► 若 RdRepeat=1 ──► RdAddr := RdPacketStart (拨回包首) 重读
                                          RdRepeat := 0
```

#### 4.4.3 源码精读

**(a) 用 FeatureSet 把不支持的功能强制清零。** 这是「同一份代码、不同档位」的关键。组合进程开头把 `OutNext_v`/`OutRepeat_v` 默认置 0，再按 FeatureSet 放行，见 [olo_base_fifo_packet.vhd:259-267](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L259-L267)：

```vhdl
OutNext_v := '0'; OutRepeat_v := '0';
if compareNoCase(FeatureSet_g, "full") then
    OutRepeat_v := Out_Repeat;                       -- 仅 FULL 放行 Repeat
end if;
if not compareNoCase(FeatureSet_g, "drop_only") then
    OutNext_v := Out_Next;                           -- DROP_ONLY 不放行 Next
end if;
```

配合实体级断言 [olo_base_fifo_packet.vhd:141-143](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L141-L143)，非法 FeatureSet 在 elaboration 阶段就报错。

**(b) 跳过的实现（Data_s 内）。** 见 [olo_base_fifo_packet.vhd:320-326](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L320-L326)：

```vhdl
-- Handle abortion of packet
if OutNext_v = '1' or r.NextLatch = '1' then
    OutLast_v := '1';                 -- 当前字提前收尾，尊重 AXI-S
    v.RdValid := '0';
    v.RdFsm   := Fetch_s;
    v.RdAddr  := r.RdPacketEnd + 1;   -- 跳到下一包首，丢弃剩余字
end if;
```

`NextLatch` 用来锁存「两次 transaction 之间」出现的 `Out_Next`，保证它不会因为恰好没握手而丢失，见 [olo_base_fifo_packet.vhd:393-397](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L393-L397)。

**(c) 重复的实现（Fetch_s 内）。** `Out_Repeat` 先锁进 `RdRepeat`（[olo_base_fifo_packet.vhd:413-415](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L413-L415)），读完包回到 `Fetch_s` 时优先检查它，把读地址拨回包首重读，见 [olo_base_fifo_packet.vhd:276-287](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L276-L287)。注意重复**不**把包从 FIFO 删除——`RdPacketStart` 不前进，`PacketLevel` 也不递减（递减条件里排除了 `RdRepeat`，见 [olo_base_fifo_packet.vhd:403-405](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L403-L405)）。

**(d) THROUGHPUT 的背靠背快路径。** `Last_s` 正常会回 `Fetch_s` 空转一拍（SPEED 模式的 stall 周期）。THROUGHPUT 模式下，若下一包的尾地址已就绪，就跳过空转、直接进 `Data_s`/`Last_s`，见 [olo_base_fifo_packet.vhd:348-366](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L348-L366)。该快路径要求「包绝不重复」，因此 `Optimization_g=THROUGHPUT` 与 `FeatureSet_g=FULL` 互斥，断言见 [olo_base_fifo_packet.vhd:153-155](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L153-L155)。

#### 4.4.4 代码实践

**目标**：分别验证读侧「跳过」与「重复」。

**操作步骤**：

1. 跑跳过用例 `NextPacketMiddle`（包 `16,17,18` 在第 nextWord 字被跳过，只读到前 nextWord+1 字），见测试台 [olo_base_fifo_packet_tb.vhd:519-532](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L519-L532)：

   ```bash
   cd sim && python3 run.py --ghdl "olo_tb.olo_base_fifo_packet_tb*NextPacketMiddle*"
   ```

   注意该用例在 `DROP_ONLY` 下会被跳过（`if FeatureSet_g /= "DROP_ONLY"`），只在 FULL/DROP_SKIP_ONLY 配置里实际运行。

2. 跑重复用例 `RepeatPacketMiddle`（同一个包被连读两遍），见测试台 [olo_base_fifo_packet_tb.vhd:423-439](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L423-L439)：

   ```bash
   python3 run.py --ghdl "olo_tb.olo_base_fifo_packet_tb*RepeatPacketMiddle*"
   ```

   该用例仅在 `FeatureSet_g=FULL` 下实际运行。

**需要观察的现象**：
- 跳过：被跳过的包提前出现 `Out_Last`，后续字不出现；`Out_Size` 仍报告**原包**大小（`pktSize => 3`），而不是被截断后的字数（见 [olo_base_fifo_packet_tb.vhd:526](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L526)）。
- 重复：同一个包的数据序列在输出端连续出现两次。

**预期结果**：两组用例均通过；`PacketLevel` 在「重复」期间不递减，在「跳过/正常读完」后才递减。

**运行结果**：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Optimization_g=THROUGHPUT` 不能和 `FeatureSet_g=FULL` 一起用？

**参考答案**：THROUGHPUT 的背靠背快路径假设「包绝不会被重复」，从而在读出时立即释放 FIFO 空间（[olo_base_fifo_packet.vhd:377-379](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L377-L379)）；而 FULL 支持 `Out_Repeat`，需要保留包数据以便重读，两者矛盾。互斥断言见 [olo_base_fifo_packet.vhd:153-155](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L153-L155)。

**练习 2**：`Out_Next` 在两次 transaction 之间（`Out_Valid=0` 时）被拉高，会丢失吗？

**参考答案**：不会。它被锁进 `NextLatch`，等下一次有效 transaction 再生效，保证按 AXI-S 规则在一次握手里给出 `Out_Last`。见 [olo_base_fifo_packet.vhd:393-397](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L393-L397)，文档说明见 [doc/base/olo_base_fifo_packet.md:222-226](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L222-L226)。

## 5. 综合实践

把四个最小模块串起来，构造一条「带 CRC 保护与选择性重传」的包通路（纯阅读 + 思考题性质，不修改源码）：

**场景**：一个外部源不断往 `olo_base_fifo_packet` 写包；你在读侧按包头类型决定「正常读 / 跳过」；某些包发送失败需要重读。

1. **选型**：根据需求选 `FeatureSet_g`。
   - 若需要重传 → 必须用 `FULL`。
   - 若只需跳过不需重传 → `DROP_SKIP_ONLY` 更省资源。
   - 若只做存储转发、连包大小都不关心 → `DROP_ONLY` 最省。
   写出你的选择并说明理由（提示：参考 [doc/base/olo_base_fifo_packet.md:58-83](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L58-L83)）。

2. **CRC 丢弃**：你在包尾算完 CRC 发现错误，于是在 `In_Last` 那一拍拉高 `In_Drop`。请回答：
   - 这个包会出现在读侧吗？（依据 4.3）
   - 此时 `In_Ready` 的行为是什么？为什么这么设计？

3. **选择性跳过**：你在包头（第 0 字）读到类型字段，发现不感兴趣，于是在第 0 字拉高 `Out_Next`。请预测：
   - `Out_Last` 会在哪一拍出现？（依据 [doc/base/olo_base_fifo_packet.md:215-217](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_packet.md#L215-L217)）
   - `Out_Size` 报告的是截断后的字数还是原包大小？（依据 4.4.4 现象）

4. **重传**：某个包读到末字时你拉高 `Out_Repeat`。请预测 `PacketLevel` 在这次「重复」前后如何变化，并说明「重复」为什么不消耗 FIFO 空间（依据 [olo_base_fifo_packet.vhd:403-405](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_packet.vhd#L403-L405)）。

5. **验证**：把上述场景对照现成测试用例——CRC 丢弃对应 `DropPacketMiddle`、跳过对应 `NextPacketMiddle`、重传对应 `RepeatPacketMiddle`、三者联合对应 `Random`（[olo_base_fifo_packet_tb.vhd:751-802](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_tb.vhd#L751-L802)）。运行它们确认你的预测。

> 参考答案要点：1) 需重传选 FULL。2) 不会出现读侧；`In_Ready` 在 `DropLatch=1` 期间恒为 1，以抽干包、避免堵塞后续包。3) `Out_Last` 立即在第 0 字出现；`Out_Size` 报告**原包**大小。4) `PacketLevel` 不变；重复不前进 `RdPacketStart`、不释放空间。

## 6. 本讲小结

- `olo_base_fifo_packet` 是**同步包 FIFO**，工作在**存储转发**模式：整包写入完成才允许读出，带来「包压缩」，但放不下的包会被自动丢弃。
- 结构上是「**大 RAM 存负载 + 小 FIFO 存包尾地址**」；`DROP_ONLY` 档用单寄存器替代小 FIFO，并把 `Last` 比特并入 RAM 以省资源。
- 包边界**完全由 `Last` 信号**界定（本实体不使用字节使能）；读侧额外给出 `Out_Size`（包字数）。
- **写侧丢包**：包内任一拍拉 `In_Drop` 即丢弃整包，写指针回卷、不入小 FIFO；`DropLatch` 期间 `In_Ready` 强制为 1 抽干包；超 `Depth_g`/`MaxPacketSize_g` 自动丢；`In_IsDropped` 回馈状态。
- **读侧跳过/重复**：`Out_Next` 提前 `Out_Last` 收尾并跳到下一包，`Out_Repeat` 把读指针拨回包首重读（不消耗空间）；两者由 `FeatureSet_g`（FULL/DROP_SKIP_ONLY/DROP_ONLY）门控。
- `Optimization_g`（SPEED/THROUGHPUT）在「时钟频率」与「背靠背吞吐」间取舍；THROUGHPUT 与 FULL 互斥。

## 7. 下一步学习建议

- 下一篇 [u3-l3 位宽转换与 TDM](u3-l3-width-conversion-tdm.md) 会讲包流在并行与 TDM 之间的转换，其中 `Last` 标记的约定与本讲一脉相承，建议连读。
- 想深入读侧 FSM 与 THROUGHPUT 快路径的取舍，可结合性能测试台 [olo_base_fifo_packet_perf_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_perf_tb.vhd) 与握手测试台 [olo_base_fifo_packet_hs_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_packet/olo_base_fifo_packet_hs_tb.vhd) 阅读吞吐与协议握手细节。
- 进阶可对比 [olo_base_fifo_async](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd)（[u3-l1](u3-l1-async-fifo.md)）理解同步与异步 FIFO 在指针同步上的差异。
