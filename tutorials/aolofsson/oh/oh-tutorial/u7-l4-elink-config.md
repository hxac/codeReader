# elink 配置子系统

## 1. 本讲目标

elink 是一条把 FPGA 与 ASIC（如 Epiphany 芯片）连起来的高速串行链路（见 u7-l1～u7-l3）。但一条裸链路没法用——软件需要在不重新综合比特流的前提下，在线地**复位通路、改写地址映射、选择路由模式、校准接收延时、读回状态**。这些「软开关」全部集中在一个叫**配置子系统**（configuration subsystem）的地方。

学完本讲，你应当能够：

- 说清 elink 配置子系统的**三层结构**：全局配置接口（`elink_cfg`）、寄存器映射（`elink_regmap.vh`）、通道级配置（`etx_cfg`/`erx_cfg`）。
- 记住 elink 的**地址位划分规则**，能根据 `elink_regmap.vh` 里的宏手算出任意寄存器的 32 位物理地址（如 `ELINK_RESET` = `0xF0200`）。
- 理解配置包如何从 TX 侧**跨时钟域**送到 RX 侧（`ecfg_cdc` 这个 `oh_fifo_cdc`）。
- 分清两种「配置」：**编译期**（`elink_constants.vh` 的 `CFG_TARGET`，选厂商原语）与**运行期**（软件写寄存器），并知道哪些运行期配置其实**尚未接通**。
- 能对照 README 的寄存器表读懂 RTL，并识别「文档声称可实现、代码其实没接」的落差。

本讲依赖 u7-l1（elink 总体架构与 IO 协议）和 u6-l1（`.vh` 寄存器映射模式）。请确认你已掌握 emesh 104 位包格式、`access/wait` 握手、以及 `dstaddr` 译码产生 one-hot 写选通这套范式。

## 2. 前置知识

本讲用到的几个概念，大多在前面讲义出现过，这里只做最小回顾：

- **寄存器映射（register map / regmap）**：把一段地址空间切成一个个寄存器，每个寄存器分一个地址编号，软硬件共用同一张表。OH! 的家规是用 `xxx_regmap.vh` 头文件里的大写宏来定义这些编号（u6-l1）。
- **地址译码与写选通**：从包里的 `dstaddr` 切出一段「寄存器号」位段，和宏比较，产生单拍的写脉冲（`xxx_write`），再用它驱动 `always` 块更新寄存器（u6-l1、u6-l2）。
- **跨时钟域（CDC）**：TX 与 RX 跑在不同的分频时钟上，信号跨域必须经同步器或异步 FIFO（u2-l4、u3-l2）。
- **soft/hard 双实现与 `CFG_TARGET`**：同一份功能用字符串参数 + `generate if` 在不同厂商实现间切换（u1-l4、u9-l1）。本讲会看到 `CFG_TARGET` 作为编译期选择器。
- **sticky（粘滞）状态位**：状态寄存器一旦被置位就用「或」累加，只有软件重写才清除——用来捕捉瞬时事件（u3-l3 的归约思想）。

一个本讲特有的术语：**配置「拦截」（tap/gate）**。`elink_cfg` 像一个夹在数据通路上的分流器：它从 TX 写通道里**认出**属于配置的写事务，把它们**偷走**去更新寄存器，剩下的事务原封不动放行给后续 FIFO。这个「偷写」机制是理解整条配置链的钥匙。

## 3. 本讲源码地图

本讲涉及的关键文件，全部位于 `elink/hdl/`：

| 文件 | 作用 | 在配置子系统中的角色 |
|------|------|----------------------|
| [elink/hdl/elink_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh) | 寄存器映射头文件 | **地址空间的事实源**：定义地址位划分、分组、每个寄存器的编号宏 |
| [elink/hdl/elink_constants.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_constants.vh) | 编译期常量头文件 | 只有 `CFG_TARGET`，被时钟模块用来选厂商原语 |
| [elink/hdl/elink_cfg.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v) | 全局/链路级配置 | 复位、时钟、chipid 三个全局寄存器；拦截 txwr 写 |
| [elink/hdl/etx_cfg.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v) | TX 通道配置 | TX 通道寄存器（MMU/重映射/路由/突发/GPIO 模式）+ 监视计数器 |
| [elink/hdl/erx_cfg.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_cfg.v) | RX 通道配置 | RX 通道寄存器（测试/MMU/重映射/IDELAY 校准/邮箱中断）+ 读回 |
| [elink/hdl/ecfg_if.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/ecfg_if.v) | （未使用）统一配置接口 | **架构意图但未接线**，本讲会作为对照说明 |
| [elink/hdl/elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v) | 顶层 | 把三个配置模块和 `ecfg_cdc` 跨域 FIFO 连起来 |

辅助参照：[elink/hdl/etx_core.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_core.v)（`etx_cfg` 的实际例化位置）、[elink/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md)（寄存器表文档）、[elink/dv/build.sh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/build.sh)（把 `elink_constants.vh` 放进 include 路径的编译脚本）。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**配置接口**（4.1）、**regmap**（4.2）、**通道配置**（4.3）。三者关系是：`regmap` 定义「地址怎么切」，配置接口和通道配置模块「按切的地址去译码、去更新寄存器、去读回」。

### 4.1 配置接口：elink_cfg 与跨域转送

#### 4.1.1 概念说明

elink 的配置入口很特别：**软件不是走一条专用配置总线，而是直接复用 TX 写通道（txwr）**。也就是说，CPU 往「某个特定地址」写一个 emesh 写事务，这个事务混在普通数据写里一起进入 elink，由配置子系统在路上把它**识别并拦截**下来。

这样设计的好处是省一组引脚、省一套接口——emesh 包格式本身就是统一的访问语言（u5-l1）。代价是配置子系统必须能从数据流里精确地「认出自己人」，而且不能让配置写事务真的被当成数据发到对端去。

这里有三件事要做：

1. **拦截**：`elink_cfg` 从 txwr 里偷走属于全局配置（复位/时钟/chipid）的写。
2. **放行**：剩下的事务照常进入 TX 数据通路；其中 TX 通道寄存器的写由 `etx_cfg` 在后面处理。
3. **跨域转送**：因为 RX 跑在另一个时钟域，配置（以及读请求的回读数据）必须经一个异步 FIFO `ecfg_cdc` 从 TX 域送到 RX 域。

> ⚠️ **现实落差（本讲最重要的诚实结论之一）**：`elink/hdl/ecfg_if.v` 是一个参数化的「统一配置接口」模块（用参数 `RX` 取 0/1 同时服务收发两侧），README 的方框图里也画了 `erx_cfgif`/`etx_cfgif`。但用全仓库搜索可以确认：**`ecfg_if` 从未被任何 RTL 实例化**，它只出现在自己的定义文件里。真正接进 `elink.v` 的是 `elink_cfg` + `etx_cfg` + `erx_cfg` 三个分立模块。这是「文档/方框图滞后于代码」的又一例，阅读时**以 `elink.v` 的实例化为准**。

#### 4.1.2 核心流程

配置写在 elink 内部的流转（TX 侧同一时钟域内 + 一次跨域）：

```
CPU 写事务 (txwr_access + txwr_packet, 目标地址 = ID|配置寄存器地址)
        │
        ▼
   ┌─────────┐  偷走 E_RESET/E_CLK/E_CHIPID 的写
   │elink_cfg│──────────────► etx_soft_reset / erx_soft_reset / clk_config / chipid
   └────┬────┘
        │ txwr_gated_access（配置写已剔除，其余放行）
        ▼
   etx ─► etx_arbiter(仲裁) ─► etx_cfg 处理 TX 通道寄存器
                                  │
                                  │ etx_cfg_access / etx_cfg_packet（读回 + 写转发）
                                  ▼
                          ┌──────────┐  跨时钟域 (tx_lclk_div4 → rx_lclk_div4)
                          │ ecfg_cdc │  = oh_fifo_cdc, DW=104, DEPTH=32
                          └────┬─────┘
                               ▼
                          erx_cfg 处理 RX 通道寄存器 + 产生读回包
```

关键点：`elink_cfg` 是一个**透明分流器**——它不阻断 txwr，只是在输出 `txwr_gated_access` 时把配置写**减掉**。

#### 4.1.3 源码精读

先看 `elink_cfg` 的端口与全局职责。它只盯着 txwr 通道，输出三个全局信号与一个「门控后的 access」：

[elink/hdl/elink_cfg.v:L2-L8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L2-L8) 定义模块，输入是 `clk/nreset/txwr_access/txwr_packet`，输出含 `txwr_gated_access/etx_soft_reset/erx_soft_reset/clk_config/chipid`。

第一步：用 `packet2emesh` 把 104 位包拆回字段，取出地址、数据、写位（这个子模块在 u7-l2/u7-l3 已见过）：

```verilog
packet2emesh #(.AW(32)) pe2 (
    .write_in   (mi_we),
    .dstaddr_in (mi_addr[31:0]),
    .data_in    (mi_din[31:0]),
    .packet_in  (txwr_packet[PW-1:0]));
```
（[elink/hdl/elink_cfg.v:L59-L70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L59-L70)）

第二步：地址译码——判断「这个 txwr 是不是写给我的」。`elink_cfg` 只认地址 `addr[10:8]==3'h2`（即 TX 寄存器组，见 4.2），且链路 ID `addr[31:20]==ID`：

```verilog
assign mi_en = txwr_access & (mi_addr[31:20]==ID) & (mi_addr[10:8]==3'h2);
assign ecfg_write = mi_en &  mi_we;   // 写
assign ecfg_read  = mi_en & ~mi_we;   // 读
```
（[elink/hdl/elink_cfg.v:L75-L82](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L75-L82)）

> 注意：`elink_cfg` 的译码**没有**检查 `addr[19:16]==EGROUP_MMR`，比 `etx_cfg`/`erx_cfg` 宽松。它只靠「寄存器组号 [10:8]==2 + 链路 ID」来认人，靠后续的寄存器号（0/1/2）来区分复位/时钟/chipid。这是一个小的不一致，读码时以源码为准。

第三步：产生三个写选通，并把它们从 txwr 里「减掉」——这就是**拦截**的核心：

```verilog
assign ecfg_reset_write  = ecfg_write & (mi_addr[RFAW+1:2]==`E_RESET);
assign ecfg_clk_write    = ecfg_write & (mi_addr[RFAW+1:2]==`E_CLK);
assign ecfg_chipid_write = ecfg_write & (mi_addr[RFAW+1:2]==`E_CHIPID);

assign txwr_gated_access = txwr_access & ~(ecfg_reset_write |
                                           ecfg_clk_write   |
                                           ecfg_chipid_write);
```
（[elink/hdl/elink_cfg.v:L85-L94](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L85-L94)）

其中 `RFAW=6`，所以 `mi_addr[RFAW+1:2]` 即 `mi_addr[7:2]`——正是寄存器号那 6 位（见 4.2 的地址划分）。`txwr_gated_access` 在 `elink.v` 里被当作模板替换喂给 `etx` 的 `.txwr_access`（[elink/hdl/elink.v:L228](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L228)），所以配置写不会污染数据 FIFO。

第四步：三个寄存器本体。复位寄存器是异步复位（`negedge nreset`），两位分别控制 TX/RX 软复位：

```verilog
always @(posedge clk or negedge nreset)
  if(!nreset) ecfg_reset_reg[1:0] <= 'b0;
  else if (ecfg_reset_write) ecfg_reset_reg[1:0] <= mi_din[1:0];
assign etx_soft_reset = ecfg_reset_reg[0];
assign erx_soft_reset = ecfg_reset_reg[1];
```
（[elink/hdl/elink_cfg.v:L99-L106](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L99-L106)）

时钟寄存器与 chipid 类似，但时钟寄存器有一句扎眼的注释：

```verilog
//TODO: implement!
always @(posedge clk or negedge nreset)
 if(!nreset) ecfg_clk_reg[15:0] <= 16'h573; //all clocks on at lowest speed
 else if (ecfg_clk_write) ecfg_clk_reg[15:0] <= mi_din[15:0];
assign clk_config[15:0] = ecfg_clk_reg[15:0];
```
（[elink/hdl/elink_cfg.v:L111-L118](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L111-L118)）

`clk_config` 虽然算出来了，但在 `elink.v` 里**悬空未接**：

```verilog
elink_cfg #(.ID(ID)) elink_cfg (.clk(sys_clk), .nreset(sys_nreset),
                                .clk_config(()),   // ← 空连接！
                                ...);
```
（[elink/hdl/elink.v:L126-L130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L126-L130)）

这正是 README 里 `ELINK_CLK (0xF0204)` 标注 `(NOT IMPLEMENTED)` 的代码依据：**运行时改时钟频率这条路当前没打通**，频率其实由 MMCM 的编译期参数决定（u7-l2）。

最后看跨域转送。TX 侧的 `etx_cfg_access/etx_cfg_packet` 经一个 `oh_fifo_cdc` 送到 RX 侧，实例名 `ecfg_cdc`：

```verilog
oh_fifo_cdc #(.DW(104), .DEPTH(32), .TARGET(TARGET)) ecfg_cdc (
    .nreset    (erx_nreset),
    .wait_out  (etx_cfg_wait),       // 反压回 etx
    .access_out(erx_cfg_access),     // 送到 erx
    .packet_out(erx_cfg_packet),
    .clk_in    (tx_lclk_div4),       // TX 慢时钟域
    .access_in (etx_cfg_access),
    .packet_in (etx_cfg_packet),
    .clk_out   (rx_lclk_div4),       // RX 慢时钟域
    .wait_in   (erx_cfg_wait));
```
（[elink/hdl/elink.v:L237-L249](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L237-L249)）

这个 FIFO 就是 u3-l2 讲过的异步 FIFO（valid/ready 握手、`wait_out` 取满标志的反）。104 位正好是一个 emesh 包宽，所以「配置/读回」和「数据」一样以整包形式跨域。

#### 4.1.4 代码实践

**实践目标**：验证「`elink_cfg` 是 txwr 上的透明分流器」这一论断。

**操作步骤（源码阅读型）**：

1. 打开 [elink/hdl/elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v)，找到 `elink_cfg` 的实例化（L126 附近），确认它的输入是原始 `txwr_access`/`txwr_packet`。
2. 找到 `etx` 的实例化，看 `AUTO_TEMPLATE` 注释（L188 附近）里写明 `.txwr_access(txwr_gated_access)`——即 etx 拿到的是**门控后**的 access。
3. 在 [elink/hdl/elink_cfg.v:L92-L94](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L92-L94) 确认 `txwr_gated_access = txwr_access & ~(三个配置写)`。

**需要观察的现象 / 预期结果**：在脑中（或仿真中）构造一个写 `ELINK_RESET (0xF0200)` 的事务：它在 `elink_cfg` 里使 `ecfg_reset_write=1`，于是 `txwr_gated_access` 在这一拍被压成 0，etx 的 txwr FIFO **不会**收到这个写；同时 `ecfg_reset_reg` 被更新，`etx_soft_reset`/`erx_soft_reset` 电平翻转。而非配置地址的普通写事务，`ecfg_*_write` 全为 0，`txwr_gated_access` 等于 `txwr_access`，**原样放行**。

> 待本地验证：由于 `packet2emesh` 子模块在当前仓库无定义（与 u7-l2/u7-l3 同样的遗留问题），`elink_cfg` 不能脱离仿真平台库替换直接编译。本实践以源码阅读为主；若要仿真，需借助 `elink/dv/build.sh` 的库搜索机制补齐子模块。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `elink_cfg` 把配置写从 txwr 里「减掉」，而不是直接让 etx 也参与译码、各自抢？

**参考答案**：因为 `ELINK_RESET`/`ELINK_CLK`/`ELINK_CHIPID` 是**全局/链路级**寄存器，它们的写动作（拉软复位、改 chipid）必须在数据进入 TX FIFO **之前**生效；如果放行到 etx 再处理，写事务会被当成数据塞进 FIFO、甚至发到对端，且复位时序也来不及。前置拦截保证了「配置归配置、数据归数据」的干净切分，也让 `etx_cfg` 只关心 TX 通道自身的寄存器。

**练习 2**：`ecfg_cdc` 用了 `oh_fifo_cdc`（DW=104）。为什么配置通路要用一个 32 深的异步 FIFO，而不是两级同步器？

**参考答案**：配置通路传的是**整包 104 位 + access 握手**，不是单比特电平；而且配置写可能连续到来（如初始化序列写多个寄存器）。两级同步器只适合单比特/慢信号，无法承载宽数据与 valid/ready 反压。异步 FIFO 既能安全跨域，又能用 `wait_out` 做反压、吸收突发。这是 u3-l2「脉冲跨域用 FIFO、电平跨域用同步器」原则的应用。

---

### 4.2 regmap：地址映射与编译期常量

#### 4.2.1 概念说明

elink 的所有可配置寄存器共用一张统一的 32 位地址表，这张表的定义只有一个地方：`elink_regmap.vh`。它是「软硬件之间的契约」——软件驱动按这张表算地址去写，硬件按这张表译码。

这张表的设计哲学和 u6-l1 的 GPIO/EDMA 寄存器映射**完全同构**：用带 `ifndef/define/endif` 守卫的大写宏给每个寄存器分配编号。区别只是 elink 的地址空间更大、分层更多——它要把「链路选择、功能分组、RX/TX 区分、寄存器号」全部塞进 32 位地址里。

另一个容易混淆的点：除了 `elink_regmap.vh`（**运行期**寄存器映射），还有一个 `elink_constants.vh`（**编译期**常量）。两者都叫「elink_...」。本模块把它们放一起讲，正是为了让你分清「运行期可改」与「综合时定死」。

#### 4.2.2 核心流程

elink 的 32 位地址按下表切分（注释在 [elink/hdl/elink_regmap.vh:L6-L12](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L6-L12)）：

| 位段 | 字段 | 含义 |
|------|------|------|
| `[31:20]` | LINKID | 链路 ID，选哪条 elink（如 `0x810`） |
| `[19:16]` | GROUP | 功能大组：`F`=MMR（寄存器）、`E`=MMU、`D`=读响应(RR) |
| `[15]` | MMU RX/TX | 仅 MMU 用：1=RX 侧，0=TX 侧 |
| `[14:11]` | （MMU 专用） | 仅 MMU 用 |
| `[10:8]` | 寄存器组 | `2`=TX、`3`=RX、`5`=DMA、`7`=MESH（邮箱）… |
| `[7:2]` | 寄存器号 | 0..63，具体寄存器 |
| `[1:0]` | 忽略 | 不支持字节访问 |

给定一个寄存器，它的物理地址可写成：

\[
\text{addr} = (\text{LINKID} \ll 20)\;\big|\;(\text{GROUP} \ll 16)\;\big|\;(\text{RGROUP} \ll 8)\;\big|\;(\text{REGNO} \ll 2)
\]

例如 `ELINK_RESET`：GROUP=`F`(MMR)、RGROUP=`2`(TX)、REGNO=`0`(`E_RESET`)，所以低 20 位 = `F_2_00` = `0xF0200`，加上链路 ID `0x810<<20` 得到 `0x810F0200`——与 README 的说明一字不差（[elink/README.md:L178](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L178)）。

注意译码时 RTL 切的是 `[7:2]` 这 6 位（即 `dstaddr[RFAW+1:2]`，`RFAW=6`），正好对应宏的 `6'd` 值。这跟 u6-l1 里「切片位宽必须与宏位宽一致」的要求完全一样。

#### 4.2.3 源码精读

**（a）大组与寄存器组的宏。** 大组用 `addr[19:16]` 选：

```verilog
`define EGROUP_MMR     4'hF // reserved for registers
`define EGROUP_MMU     4'hE // MMU RX([15]==1), TX([15]==0)
`define EGROUP_RR      4'hD // read response block
```
（[elink/hdl/elink_regmap.vh:L15-L17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L15-L17)）

寄存器组用 `addr[10:8]` 选，宏值正好是 8 字节步长：

```verilog
`define EGROUP_TX       3'd2 //0x200
`define EGROUP_RX       3'd3 //0x300
`define EGROUP_DMA      3'd5 //0x500
`define EGROUP_MESH     3'd7 //0x700
```
（[elink/hdl/elink_regmap.vh:L20-L26](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L20-L26)）

**（b）TX/RX 寄存器编号。** TX 组里的寄存器（注释直接给出绝对地址，便于核对）：

```verilog
`define E_RESET        6'd0 //F0200-reset
`define E_CLK          6'd1 //F0204-clock configuration
`define E_CHIPID       6'd2 //F0208-Epiphany chip id for colid/rowid pins
`define E_VERSION      6'd3 //F020C-version #
`define ETX_CFG        6'd4 //F0210-config
`define ETX_STATUS     6'd5 //F0214-tx status
...
```
（[elink/hdl/elink_regmap.vh:L29-L37](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L29-L37)）

RX 组里的寄存器（注意 RX 组的 `ERX_CFG=6'd0`，绝对地址是 `F0300`，因为 RGROUP=`3`）：

```verilog
`define ERX_CFG        6'd0 //F0300-config
`define ERX_STATUS     6'd1 //F0304-status register
`define ERX_GPIO       6'd2 //F0308-sampled data
`define ERX_OFFSET     6'd3 //F030C-memory base for remap
`define ERX_IDELAY0    6'd4 //F0310-tap delay for d[5:0]
`define ERX_IDELAY1    6'd5 //F0314-tap delays for {frame,d[7:6]}
`define ERX_TESTDATA   6'd6 //F0318-received test data
```
（[elink/hdl/elink_regmap.vh:L40-L46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L40-L46)）

**（c）跨头文件共享符号的二级守卫。** 邮箱寄存器宏被一层额外的 `ifndef E_MAILBOXLO` 包住：

```verilog
`ifndef  E_MAILBOXLO
 `define E_MAILBOXLO   6'hC //F0730-lower 32 bits of mailbox
 `define E_MAILBOXHI   6'hD //F0734-upper 32 bits of mailbox
 `define E_MAILBOXSTAT 6'hE //F0738-mailbox status
`endif
```
（[elink/hdl/elink_regmap.vh:L49-L53](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L49-L53)）

这是 u6-l1 提到的「符号级守卫」：因为 `emailbox` 子模块自己的 regmap 也可能定义同名宏，当两个 `.vh` 同时被 include 时，这层守卫避免重复定义报错。这是 OH! 处理多 IP 复用同名寄存器编号的标准技巧。

**（d）编译期常量 `elink_constants.vh`。** 这个文件极小，全文只有一个宏：

```verilog
`ifndef ELINK_CONSTANTS_V_
 `define ELINK_CONSTANTS_V_
 `define CFG_TARGET "XILINX"  // default hard macro target
                              // see also "GENERIC", "ALTERA", "ASIC"
`endif
```
（[elink/hdl/elink_constants.vh:L1-L6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_constants.vh#L1-L6)）

它被 `etx_clocks.v`/`erx_clocks.v` 用来给 `TARGET` 参数赋默认值（`parameter TARGET = `CFG_TARGET;`，见 [elink/hdl/etx_clocks.v:L15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v#L15)），从而在**综合时**选择 XILINX/ALTERA/ASIC 的具体时钟原语。它是**编译期**配置，不是软件可写的寄存器——这是本讲必须分清的关键。

`elink/dv/build.sh` 把它放进 iverilog 的命令行，确保仿真时 `CFG_TARGET` 有定义：

```bash
CFG="../hdl/elink_constants.vh"
iverilog -g2005 -DTARGET_SIM=1 $CFG $top dut_${dut}.v -f ../../common/dv/libs.cmd -o ${dut}.vvp $1
```
（[elink/dv/build.sh:L5-L6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/build.sh#L5-L6)）

#### 4.2.4 代码实践

**实践目标**：本讲义规格要求的实践——阅读 `elink_constants.vh`，列出影响 TX 时钟频率与 DDR 模式的关键配置位。这里给出**忠于源码**的答案（而非 README 的理想描述）。

**操作步骤**：

1. 读 [elink/hdl/elink_constants.vh:L1-L6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_constants.vh#L1-L6)，确认它只定义了 `CFG_TARGET`。
2. 在仓库内搜索 `CFG_TARGET` 的使用点，确认它被 `etx_clocks.v`/`erx_clocks.v` 当作 `TARGET` 参数默认值。
3. 对照 [elink/README.md:L216-L247](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L216-L247) 的 `ELINK_CLK` 字段表。

**需要观察的现象 / 预期结果**——诚实的结论分三层：

- **`elink_constants.vh` 本身不包含任何「时钟频率位」或「DDR 模式位」**。它只有一个字符串宏 `CFG_TARGET`，作用是**编译期**选厂商原语（XILINX→用 MMCM/IDDR/ODDR；ALTERA/ASIC→别的）。它影响「用哪种时钟硬件」，但不影响「跑多快」。
- **运行期「TX 时钟频率」名义上由 `ELINK_CLK (0xF0204)` 控制**：按 README，位 `[7:4]` 是 cclk 分频比、`[11:8]` 是 lclk 分频比、`[15:12]` 是 PLL 频率、`[0]/[1]` 是时钟使能、`[2]/[3]` 是旁路选择。**但**：(1) 该寄存器在 README 标 `(NOT IMPLEMENTED)`；(2) `elink_cfg` 算出的 `clk_config` 在 `elink.v` 里悬空（`.clk_config(())`）；(3) 实际分频比由 MMCM 的**编译期参数**决定（u7-l2）。所以运行期改频率**当前不可用**。
- **DDR 模式根本没有运行期寄存器位**。DDR（双沿采样）是 `etx_io`/`erx_io` 里用 ODDR/IDDR 原语**结构上写死**的，由 `TARGET` 在综合时决定用哪种原语。不存在某个寄存器位可以「关掉 DDR」。

**一句话总结**：`elink_constants.vh` 只管「编译期选厂商」；运行期时钟频率位（`ELINK_CLK`）已定义但未接通；DDR 是硬连线的结构特性、无寄存器位。这正是「README 是愿望、RTL 是事实」的典型样本。

> 待本地验证：如需在仿真中确认 `clk_config` 确实悬空、对链路行为无影响，可在 `elink/dv` 下用 `build.sh` 编译 `dut_elink.v`，在波形里观察写 `0xF0204` 后 `txo_lclk` 频率是否变化（预期：不变化）。

#### 4.2.5 小练习与答案

**练习 1**：手算 `ELINK_RXDELAY1` 的物理地址（链路 ID 设为 `0x810`）。

**参考答案**：查 regmap，`ERX_IDELAY1=6'd5` 属于 RX 组（`EGROUP_RX=3`）在 MMR 大组（`F`）下。低 20 位 = `(F<<12)|(3<<8)|(5<<2)` = `F_3_14` = `0xF0314`，加链路 ID 得 `0x810F0314`，与 README `ELINK_RXDELAY1 | 0xF0314` 吻合。

**练习 2**：为什么 `elink_constants.vh` 里的 `CFG_TARGET` 是字符串 `"XILINX"`，而不是数字？提示：回忆 u1-l4 的 soft/hard 切换机制。

**参考答案**：OH! 用字符串参数 + `generate if(SYN=="TRUE")`（或 `if(TARGET=="XILINX")`）在同一份 RTL 里切换不同实现。字符串比数字更自文档化（`"XILINX"`/`"ALTERA"`/`"ASIC"` 一眼可读），且 Verilog 允许 `parameter` 取字符串值用于 `generate` 条件比较。`CFG_TARGET` 作为默认值传给 `TARGET` 参数，让时钟模块在综合时按厂商选出正确的原语分支——这是编译期配置，不是运行期寄存器。

**练习 3**：`elink_regmap.vh` 的地址里 `[1:0]` 被注释为「IGNORED (no byte access)」。这跟 u5-l1 讲的 emesh `datamode` 有什么关系？

**参考答案**：emesh 包的控制字节里 `datamode[2:1]` 决定数据宽度（字节/半字/字/双字），但 elink 的寄存器映射**故意不支持字节/半字粒度**——所有寄存器都按字（32 位）对齐访问，所以地址低 2 位（字节偏移）无意义、被忽略。这是一种简化：寄存器只做整字读写，译码只看 `[7:2]` 的寄存器号，降低了硬件复杂度。

---

### 4.3 通道配置：etx_cfg（TX）与 erx_cfg（RX）

#### 4.3.1 概念说明

全局寄存器（复位/时钟/chipid）由 `elink_cfg` 处理，剩下的「与某一侧数据通路强相关」的寄存器，分给两个通道级配置模块：

- **`etx_cfg`**（TX 侧）：控制 TX 数据通路的行为——是否启用 MMU 地址翻译、是否重映射目标地址、强制路由方向（ctrlmode）、是否允许突发、是否进入 GPIO 直驱模式；同时维护 TX 的状态/监视/包采样寄存器。它还负责把配置包**转发给 RX**（经 4.1 的 `ecfg_cdc`）。
- **`erx_cfg`**（RX 侧）：控制 RX 数据通路——测试模式、RX MMU 使能、RX 地址重映射（静态/动态）、IDELAY 延时校准、邮箱中断使能；同时是邮箱/DMA/MMU 读回数据的汇聚点。

两者的共同范式：`packet2emesh` 解包 → 按 `addr[19:16]` 大组和 `addr[10:8]` 寄存器组译码 → 产生写选通更新寄存器 → `case` 选读回数据 → `emesh2packet` 重打包。这与 u6-l2 的 GPIO 完全同构，只是寄存器更多、字段更丰富。

#### 4.3.2 核心流程

**TX 侧（`etx_cfg`，在 `etx_core.v` 内例化）**：

```
etx_packet ─► packet2emesh ─► 解字段
                                │
   tx_match = cfg_access & (addr[19:16]==MMR) & (addr[10:8]==TX)
   cfg_mmu_access = cfg_access & (addr[19:16]==MMU) & ~addr[15]   ← TX 侧 MMU
                                │
   写选通: tx_version/tx_cfg/tx_status/tx_gpio/tx_monitor_write
                                │
   tx_cfg_reg[15:0] ──字段拆分──► tx_enable/mmu_enable/remap_enable/
                                   ctrlmode/ctrlmode_bypass/burst_enable/gpio_enable
                                │
   读回: case(addr) ─► cfg_dout ─► emesh2packet ─► etx_cfg_packet ─► ecfg_cdc ─► RX
```

**RX 侧（`erx_cfg`）**：

```
erx_cfg_packet（来自 ecfg_cdc）─► packet2emesh ─► 解字段
                                │
   cfg_access:      MMR & RX组(3)
   mailbox_access:  MMR & MESH组(7)
   dma_access:      MMR & DMA组(5)
   mmu_access:      MMU组 & addr[15]   ← RX 侧 MMU
                                │
   写选通: rx_cfg/rx_offset/rx_idelay0/rx_idelay1/rx_testdata/rx_status_write
                                │
   rx_cfg_reg[31:0] ──字段拆分──► test_mode/mmu_enable/remap_mode/remap_sel/
                                   remap_pattern/mailbox_irq_en
   idelay寄存器 ──重排──► idelay_value[44:0] + load_taps（加载到 IDELAY 原语）
                                │
   读回: case(addr)─cfg_rdata ┐
         mailbox_rdata         ├─► oh_mux4 ─► emesh2packet ─► ecfg_packet（回 axi_slave）
         edma_rdata             │
         tx转发数据 ────────────┘
```

注意 RX 侧的读回用了一个 `oh_mux4`（u2-l1 的 one-hot 选择器）来在「本组寄存器 / 邮箱 / DMA / TX 转发」四路读回数据里选其一——因为不同大组（MMU/DMA/MESH/RR）的读回数据来源不同。

#### 4.3.3 源码精读

**（a）TX 通道配置 `etx_cfg`。** 译码逻辑（注意它**有**检查 `addr[19:16]==MMR`，比 `elink_cfg` 严格）：

```verilog
assign tx_match      = cfg_access & (dstaddr_in[19:16]==`EGROUP_MMR) & (dstaddr_in[10:8]==`EGROUP_TX);
assign cfg_mmu_access= cfg_access & (dstaddr_in[19:16]==`EGROUP_MMU) & ~dstaddr_in[15]; // TX 侧 MMU
assign ecfg_read     = tx_match & ~write_in;
assign ecfg_write    = tx_match &  write_in;
```
（[elink/hdl/etx_cfg.v:L103-L113](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L103-L113)）

写选通按寄存器号展开：

```verilog
assign tx_version_write = ecfg_write & (dstaddr_in[RFAW+1:2]==`E_VERSION);
assign tx_cfg_write     = ecfg_write & (dstaddr_in[RFAW+1:2]==`ETX_CFG);
assign tx_status_write  = ecfg_write & (dstaddr_in[RFAW+1:2]==`ETX_STATUS);
...
```
（[elink/hdl/etx_cfg.v:L118-L122](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L118-L122)）

`tx_cfg_reg` 是 TX 通路行为的总开关，字段拆分与 README `ELINK_TXCFG` 表一一对应：

```verilog
always @(posedge clk)
  if(!nreset) tx_cfg_reg[15:0] <= 'b0;
  else if (tx_cfg_write) tx_cfg_reg[15:0] <= data_in[15:0];

assign tx_enable       = 1'b1;                       // TODO: fix!  ← 位[0]未实现
assign mmu_enable      = tx_cfg_reg[1];
assign remap_enable    = (tx_cfg_reg[3:2]==2'b01);
assign ctrlmode[3:0]   = tx_cfg_reg[7:4];
assign ctrlmode_bypass = tx_cfg_reg[9];
assign burst_enable    = tx_cfg_reg[10];
assign gpio_enable     = (tx_cfg_reg[12:11]==2'b01);
```
（[elink/hdl/etx_cfg.v:L127-L139](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L127-L139)）

> 又一个「文档 vs 代码」的落差：README 说 `ELINK_TXCFG[0]` 是「reserved for TX enable」，代码里直接 `tx_enable = 1'b1` 并留 `TODO: fix!`——TX 使能当前**恒为 1**，那个位并没有生效。

状态寄存器是 **sticky** 的：写时覆盖，不写时把同步过来的状态位「或」进去，捕捉瞬时事件：

```verilog
oh_dsync isync[15:0] (.dout(tx_status_sync[15:0]), .clk(clk), .nreset(1'b1), .din(tx_status[15:0]));

always @(posedge clk)
  if (tx_status_write) tx_status_reg[15:0] <= data_in[15:0];
  else                 tx_status_reg[15:0] <= tx_status_reg[15:0] | {tx_status_sync[15:0]};
```
（[elink/hdl/etx_cfg.v:L146-L155](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L146-L155)）

`oh_dsync`（u2-l4）把来自快时钟域的 `tx_status` 两级同步到慢域；`isync[15:0]` 是数组实例化，16 个同步器一字排开。监视寄存器则是个事务计数器，每完成一笔（`etx_access & ~etx_wait`）就加 1：

```verilog
always @(posedge clk)
  if (tx_monitor_write) tx_monitor_reg[31:0] <= data_in[31:0];
  else tx_monitor_reg[31:0] <= tx_monitor_reg[31:0] + (etx_access & ~etx_wait);
```
（[elink/hdl/etx_cfg.v:L178-L182](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L178-L182)）

计数增量 \(\Delta = \text{etx\_access}\;\&\;\lnot\text{etx\_wait}\) 正是「这一拍事务成立（access=1）且未被反压（wait=0）」的语义，与 u5-l1 的握手成立条件一致。读回用 `case` 选 `cfg_dout`，再经 `emesh2packet` 打包，并通过 `etx_cfg_packet` 转发给 RX（[elink/hdl/etx_cfg.v:L195-L228](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L195-L228)）。读回时 `write_out` 恒置 1——读响应在 emesh 里本质是一个「写回请求方」的事务（u5-l3 的 readback 范式）。

**（b）RX 通道配置 `erx_cfg`。** 它的输入 `erx_cfg_packet` 已经是跨域后的包。译码把不同大组分拣到 mailbox/dma/mmu/cfg 四个出口：

```verilog
assign cfg_access     = erx_cfg_access & (dstaddr_in[19:16]==`EGROUP_MMR) & (dstaddr_in[10:8]==`EGROUP_RX);
assign mailbox_access = erx_cfg_access & (dstaddr_in[19:16]==`EGROUP_MMR) & (dstaddr_in[10:8]==`EGROUP_MESH);
assign dma_access     = erx_cfg_access & (dstaddr_in[19:16]==`EGROUP_MMR) & (dstaddr_in[10:8]==`EGROUP_DMA);
assign mmu_access     = erx_cfg_access & (dstaddr_in[19:16]==`EGROUP_MMU) & dstaddr_in[15];  // RX 侧 MMU
```
（[elink/hdl/erx_cfg.v:L110-L124](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_cfg.v#L110-L124)）

注意 TX 与 RX 的 MMU 用 `addr[15]` 区分：`~addr[15]` 是 TX MMU、`addr[15]` 是 RX MMU（与 regmap 注释 `[15] MMU SELECT (for RX/TX)` 一致）。`rx_cfg_reg` 的字段拆分对应 README `ELINK_RXCFG`：

```verilog
assign test_mode           = rx_cfg_reg[0];
assign mmu_enable          = rx_cfg_reg[1];
assign remap_mode[1:0]     = rx_cfg_reg[3:2];
assign remap_sel[11:0]     = rx_cfg_reg[15:4];
assign remap_pattern[11:0] = rx_cfg_reg[27:16];
assign mailbox_irq_en      = rx_cfg_reg[28];
```
（[elink/hdl/erx_cfg.v:L147-L158](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_cfg.v#L147-L158)）

IDELAY 校准是 RX 配置最有硬件特色的部分。`ERX_IDELAY0/1` 两个寄存器拼出 45 位的延时抽头值（frame + 8 根数据线，每根 5 位：1 位 msb + 4 位 lsb），但拼装顺序与寄存器存放顺序不同，所以要做一次重排：

```verilog
assign idelay_value[44:0] = {idelay[44],idelay[35:32], //frame
                             idelay[43],idelay[31:28], //d7
                             idelay[42],idelay[27:24], //d6
                             ...
                             idelay[36],idelay[3:0]   //d0
                             };
always @(posedge clk) load_taps <= rx_idelay1_write;
```
（[elink/hdl/erx_cfg.v:L194-L205](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_cfg.v#L194-L205)）

写 `ERX_IDELAY1` 的下一拍产生一个 `load_taps` 脉冲，把整组抽头值一次性打入 IDELAY 原语——这是 Xilinx IDELAY 的标准加载时序（u7-l3 在 `erx_io` 里用到）。这种「先攒齐、再脉冲加载」是为了避免逐位改抽头时出现瞬时毛刺。

读回用 `case` 选 `cfg_rdata`，再用 `oh_mux4` 在四路来源里选其一：

```verilog
oh_mux4 #(.DW(32)) mux4(
    .out (data_mux[31:0]),
    .in0 (cfg_rdata[31:0]),     .sel0 (rx_sel),
    .in1 (mailbox_rdata[31:0]), .sel1 (mailbox_sel),
    .in2 (edma_rdata[31:0]),    .sel2 (dma_sel),
    .in3 (data_out[31:0]),      .sel3 (tx_sel));  // TX 转发来的读回
```
（[elink/hdl/erx_cfg.v:L255-L261](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_cfg.v#L255-L261)）

`tx_sel` 那一路（`in3`）是关键：TX 侧的读请求（如读 `ELINK_TXCFG`）其数据在 TX 域，但最终响应要经 `ecfg_cdc` 流到 RX，再由 `erx_cfg` 汇聚、打回给请求方。所以 `erx_cfg` 既是 RX 的配置模块，也是**整条 elink 读回响应的总汇聚点**。

#### 4.3.4 代码实践

**实践目标**：把一个寄存器位的「软件写法 → RTL 生效路径」完整走一遍，体会「写一个字如何改变链路行为」。

**操作步骤（源码阅读型 + 可选仿真）**：

1. **选定一个真实可用的配置位**：`ELINK_TXCFG[10]`（`burst_enable`，突发模式）。
2. **查地址**：在 [elink/hdl/elink_regmap.vh:L33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L33) 确认 `ETX_CFG=6'd4`，手算地址 = `0xF0210`（链路 ID 视具体平台拼接）。
3. **跟踪写路径**：软件写 `0xF0210` 数据 `0x0400`（bit10=1）→ 进入 txwr → `elink_cfg` 不拦（它只拦 reg 0/1/2）→ `txwr_gated_access` 放行 → `etx_arbiter` 仲裁 → `etx_cfg` 在 [elink/hdl/etx_cfg.v:L119](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L119) 命中 `tx_cfg_write` → [L138](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L138) `burst_enable` 拉高 → 喂给 `etx_protocol`（在 `etx_core.v` [L189](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_core.v#L189) 的 `.burst_enable(burst_enable)`），使后续连续地址写合并成一次突发帧。
4. **可选仿真**：用 [elink/dv/build.sh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/build.sh) 编译 `dut_elink.v`，构造一个向 `0x810F0210` 写 `0x0400` 的 `.emf` 事务，观察 `burst_enable` 信号（在 `etx_core` 内）从 0 变 1。

**需要观察的现象 / 预期结果**：写 `0x0400` 到 `ELINK_TXCFG` 后，`burst_enable` 在下一拍变为 1；之后再发连续地址的写事务，应能看到 `etx_protocol` 的 `tx_burst` 被置位、FRAME 不再每笔都重新打起始沿（u7-l2 的突发检测）。

> 待本地验证：完整仿真需补齐 `packet2emesh`/`emesh2packet` 等子模块（仓库内缺失，见 u7-l2 的说明），建议借助 `elink/dv/build.sh` 的库搜索路径。若仅做源码阅读，第 1～3 步已足够验证「地址→字段」的映射正确性。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `erx_cfg` 的读回要用 `oh_mux4`，而 `etx_cfg` 用一个简单的 `data_mux = read_sel ? cfg_dout : data_out` 二选一就够了？

**参考答案**：因为 `erx_cfg` 是**整条 elink 的读回总汇聚点**——它要同时服务 RX 自身寄存器、邮箱、DMA、以及从 TX 跨域转发来的读回，共四路来源，所以用 4 选 1 的 `oh_mux4`。而 `etx_cfg` 只需在「TX 本地寄存器读回」与「转发原始数据」二选一，所以一个三目运算即可。这是「读回来源数量」决定的选择器规模。

**练习 2**：TX MMU 与 RX MMU 用同一个大组 `EGROUP_MMU`，硬件如何区分一次 MMU 访问属于 TX 还是 RX？

**参考答案**：用 `addr[15]` 这一位（regmap 注释 `[15] MMU SELECT (for RX/TX)`）。`etx_cfg` 译码 `cfg_mmu_access = ... & ~dstaddr_in[15]`（bit15=0 → TX 侧），`erx_cfg` 译码 `mmu_access = ... & dstaddr_in[15]`（bit15=1 → RX 侧）。一个地址位把 MMU 表空间一分为二，分别挂到收发两侧的 `emmu` 实例（u6-l4）。

**练习 3**：`erx_cfg` 里 `load_taps <= rx_idelay1_write` 把一个「写脉冲」延迟一拍再输出。为什么不在写脉冲**同一拍**直接加载 IDELAY？

**参考答案**：因为 `idelay` 寄存器是在 `rx_idelay0_write`/`rx_idelay1_write` 触发的 `always` 块里更新的——在写脉冲**当拍**，新的抽头值还没真正锁存进 `idelay` 寄存器（非阻塞赋值在时钟沿后才生效）。若同拍加载，加载的还是旧值。延迟一拍后，`idelay_value` 已经稳定为新值，再用 `load_taps` 脉冲整体打入 IDELAY 原语，保证「攒齐新值、再整体加载」的无毛刺时序。

---

## 5. 综合实践

**任务**：给一位新同事写一份「elink 在线配置速查表」，要求只基于**当前 RTL 真实支持**的功能（剔除 README 里标 `NOT IMPLEMENTED` 或代码里标 `TODO` 的项）。

请完成下面三件事：

1. **地址表**：从 [elink/hdl/elink_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh) 抄出全部寄存器宏，用本讲的地址公式 \(\text{addr}=(\text{LINKID}\ll20)|(\text{GROUP}\ll16)|(\text{RGROUP}\ll8)|(\text{REGNO}\ll2)\) 算出每个寄存器的低 20 位地址，填一张「寄存器名 / 地址 / 所属模块（elink_cfg 还是 etx_cfg/erx_cfg）/ 读写性」表。

2. **可用位映射**：逐个核对以下「声称的配置位」在 RTL 里是否真接通，并用一句话给出依据：
   - `ELINK_RESET[0]`（TX 复位）、`[1]`（RX 复位）——查 [elink_cfg.v:L105-L106](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_cfg.v#L105-L106)。
   - `ELINK_CLK` 各位——查 `elink.v` 里 `.clk_config(())`。
   - `ELINK_TXCFG[0]`（TX 使能）——查 [etx_cfg.v:L133](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L133) 的 `tx_enable = 1'b1`。
   - `ELINK_TXCFG[10]`（突发）、`[12:11]`（GPIO 模式）——查 [etx_cfg.v:L138-L139](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_cfg.v#L138-L139)。
   - `ELINK_RXCFG[0]`（测试模式）、`[28]`（邮箱中断）——查 [erx_cfg.v:L153-L158](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_cfg.v#L153-L158)。
   - DDR 模式开关——在 regmap 里搜（提示：搜不到，说明无此位）。

3. **一段配置流程描述**：写 5～8 行，说明软件要「复位 RX 通路并打开邮箱中断」需要往哪两个地址写什么值，以及这两笔写分别经 `elink_cfg`、`ecfg_cdc`、`erx_cfg` 中的哪几个模块、为什么走不同路径。

**预期成果**：一份能区分「文档愿望」与「代码事实」的速查表。完成它你就真正掌握了 elink 配置子系统的三层结构、地址映射、以及「阅读时以 RTL 为准」的工程习惯。

> 待本地验证：第 2 步的核对完全可由源码阅读完成；若想验证「可用位」的实际行为，需借助 `elink/dv` 仿真环境（注意补齐缺失子模块）。

## 6. 本讲小结

- elink 的配置子系统是**三层结构**：`elink_regmap.vh` 定义地址映射、`elink_cfg` 处理全局寄存器（复位/时钟/chipid）、`etx_cfg`/`erx_cfg` 处理收发通道寄存器。
- 配置**复用 TX 写通道**：`elink_cfg` 是 txwr 上的透明分流器，用 `txwr_gated_access = txwr_access & ~(配置写)` 把全局配置写「偷走」，其余放行；TX/RX 通道寄存器由 `etx_cfg`/`erx_cfg` 各自译码。
- 因为 RX 在另一个时钟域，配置与读回经一个 `oh_fifo_cdc`（`ecfg_cdc`，DW=104）跨域转送；`erx_cfg` 是**整条链路读回响应的总汇聚点**（用 `oh_mux4` 四选一）。
- 地址 32 位按 `LINKID|GROUP|（MMU RX/TX 位）|寄存器组|寄存器号|忽略` 切分，可由 `elink_regmap.vh` 的宏手算任意寄存器地址；译码统一切 `[7:2]` 这 6 位寄存器号。
- 必须分清两种配置：`elink_constants.vh` 的 `CFG_TARGET` 是**编译期**厂商选择（被时钟模块当 `TARGET` 参数），`elink_regmap.vh` 是**运行期**寄存器映射。
- **诚实的落差**：`ecfg_if.v` 是未接线的架构意图；`ELINK_CLK`（运行期改频率）标 `NOT IMPLEMENTED`、`clk_config` 在顶层悬空；`ELINK_TXCFG[0]`（TX 使能）写死为 1；DDR 是硬连线结构、无寄存器位。README 是愿望，RTL 才是事实。

## 7. 下一步学习建议

- **横向对比 mio 的配置**：阅读 [mio/hdl/cfg_mio.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/cfg_mio.vh) 与 `mio/hdl/mio.v`，对比 mio 与 elink 的寄存器映射风格异同（mio 同样有 `CFG_TARGET`，但默认 `"GENERIC"`）。这是 u8-l4 的前置。
- **纵向深入时钟实现**：本讲看到 `clk_config` 悬空、频率实由 MMCM 决定。若想搞清「频率到底怎么定」，精读 [elink/hdl/etx_clocks.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_clocks.v) 与 [elink/hdl/erx_clocks.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v) 里 `TARGET=="XILINX"` 分支下的 MMCM/PLL 参数，回看 u7-l2/u7-l3。
- **系统级视角**：继续学 u8-l1（AXI 协议与 emaxi 主桥）和 u8-l2（esaxi 从桥与 axi_elink 桥接），看 CPU 侧的 AXI 事务如何变成对 elink 寄存器的写——你会再次遇到本讲的 `packet2emesh` 与 `elink_regmap.vh` 地址。
- **亲手扩展**：作为高级练习，尝试仿照 `erx_cfg` 的 `rx_cfg_reg` 字段拆分，在纸上为 elink 设计一个「运行期链路休眠」寄存器位（类比 `ELINK_CLK` 但真正接通），规划它需要改动 `elink_regmap.vh`、`elink_cfg`、以及顶层连线的哪些地方——这会把本讲的三层结构彻底串起来。
