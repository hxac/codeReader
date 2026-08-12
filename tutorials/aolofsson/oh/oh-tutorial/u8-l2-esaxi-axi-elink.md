# esaxi 从桥与 axi_elink 桥接

## 1. 本讲目标

本讲承接 u8-l1（emaxi 主桥）与 u7-l1（elink 总体架构），把「AXI 总线」与「elink 高速链路」彻底缝合起来。学完后你应当能够：

- 说清 **esaxi**（AXI 从桥）的写通路、读通路、读响应组装与超时兜底是如何工作的；
- 说清 **axi_elink** 顶层如何把 `emaxi`、`esaxi`、`elink` 三个大模块用一组内部网络（net）拼成一颗完整的「AXI↔elink 双向桥芯片」；
- 画出一条事务从本地 AXI master 出发、经 elink 链路、抵达远端 emesh slave、再原路返回的完整端到端通路；
- 理解 esaxi 与 emaxi 为何是「镜像对称」关系，以及二者在数据位宽、突发、乱序处理上的取舍差异。

## 2. 前置知识

本讲默认你已掌握以下概念（若生疏请先回看对应讲义）：

- **AXI4 五通道与握手**（u8-l1）：写地址 AW、写数据 W、写响应 B、读地址 AR、读数据 R，每通道各自 `valid/ready` 握手；铁律是「valid 不许等 ready」。
- **emesh 104 位包格式**（u5-l1）：包宽 `PW = 2·AW+40`，从低位起为 `write[0]`、`datamode[2:1]`、`ctrlmode[6:3]`、`reserved[7]`、`dstaddr[39:8]`、`data[71:40]`、`srcaddr[103:72]`。`access`≈valid，`wait` 高有效表反压（`~wait`≈ready）。
- **elink 六通道划分**（u7-l1）：系统侧分 TX/RX 各 wr/rd/rr 三类事务，共 `txwr/txrd/txrr/rxwr/rxrd/rxrr` 六个独立 emesh 通道。
- **emaxi 主桥**（u8-l1）：emesh 包 ↔ AXI 主端口，代表 elink **主动**读写外部 AXI slave（如 DDR）。

一个关键直觉先建立起来：**方向**。AXI 里「master」是发起方、「slave」是响应方；但站在 elink 链路看：

| 模块 | AXI 角色 | emesh 方向 | 物理含义 |
|------|----------|-----------|----------|
| `esaxi` | **slave**（S_AXI） | 产出 txwr/txrd，消费 rxrr | 本地 CPU 经 AXI 把请求**送上网线** |
| `emaxi` | **master**（M_AXI） | 消费 rxwr/rxrd，产出 txrr | 远端经网线发来的请求被**送到本地 DDR** |

也就是说，esaxi 与 emaxi 互为镜像：一个把「AXI→emesh」、一个把「emesh→AXI」。记住这张表，后面所有连线都不会乱。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
|------|------|----------|
| [axi/hdl/esaxi.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v) | AXI **从**桥：S_AXI ↔ emesh（txwr/txrd/rxrr） | 4.1 的全部内容 |
| [elink/hdl/axi_elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v) | 顶层：实例化 elink + esaxi + emaxi，用内部 net 拼成完整桥 | 4.2 的全部内容 |
| [axi/hdl/emaxi.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v) | AXI **主**桥（u8-l1 已详讲） | 4.2 用于与 esaxi 对照 |
| [elink/hdl/elink_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh) | 定义 `EGROUP_RR` 等地址组常量 | 解释读响应回信地址 |
| [elink/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md) | 包格式、六通道、设计结构图 | 权威字段定义 |

> ⚠️ 工程现实：`esaxi.v` 与 `emaxi.v` 都实例化了 `emesh2packet` / `packet2emesh`，但全仓库**找不到这两个模块的定义**（见 4.1.3 的验证）。因此 `esaxi`、`emaxi`、`axi_elink` 目前**不能原样编译**，本讲以源码文本与 README 为事实依据，关键行为「待本地验证」。

## 4. 核心概念与源码讲解

### 4.1 AXI 从桥 esaxi

#### 4.1.1 概念说明

`esaxi` 是一颗 AXI **slave** IP。它的左侧是标准 AXI4 从端口 `s_axi_*`（接一个外部 AXI master，典型是 Zynq PS 里的 CPU），右侧是三条 emesh 通道：

- **写请求输出** `wr_access/wr_packet`（送 elink 的 txwr 通道，上网）；
- **读请求输出** `rd_access/rd_packet`（送 elink 的 txrd 通道，上网）；
- **读响应输入** `rr_access/rr_packet`（从 elink 的 rxrr 通道回来，远端的回信）。

它解决的问题是：**让一颗只会说 AXI 的本地 CPU，能够对 elink 链路对面的远端地址空间做读写。** CPU 发出的 AXI 写/读，被翻译成 emesh 包送上链路；远端处理完后，读数据再沿 rxrr 通道返回，被组装回 AXI 的 R 通道交还 CPU。

数据位宽上，`esaxi` 的 S_AXI 是 **32 位数据**（`DW=32`，`s_axi_wdata[31:0]`、`s_axi_wstrb[3:0]` 共 4 字节使能）。这与 `emaxi` 的 64 位 M_AXI 形成对照——CPU 侧窄、内存侧宽，是一种常见的不对称配置。

#### 4.1.2 核心流程

esaxi 内部可拆为四条相对独立的子通路。下面用伪代码描述其行为（高电平有效，时钟为 `s_axi_aclk`）。

**① 写通路（AXI AW+W → emesh txwr）**

```
# AW 通道：抓地址
on (awready & awvalid):  捕获 awaddr/awsize/awburst/awid;  write_active=1
# W 通道：逐拍抓数据，按 wstrb 选字节车道 + 置地址低 2 位
on (wready & wvalid):    按 wstrb 把数据左对齐到 32 位, 算 wr_dstaddr[1:0]
                          pre_wr_en <= 1
                          (下一拍) wr_access <= pre_wr_en   # 一级流水对齐
# 成帧：emesh2packet 把字段拼成 104 位 wr_packet, write 位恒 1
# WLAST：last_wr_beat 触发 B 通道响应
on (wready & wvalid & wlast):  bvalid=1; bresp=0(OKAY); write_active=0
```

注意 `pre_wr_en → wr_access` 这一拍流水：它是为了把「地址/数据译码」与「包打包」在时间上对齐，使送出的 `wr_packet` 里字段与 `wr_access` 脉冲同拍有效。

**② 读请求通路（AXI AR → emesh txrd）**

```
on (arready & arvalid):  捕获 araddr/arlen/arburst/arsize/arid;  read_active=1
                          s_axi_rlast <= (arlen==0)              # 单拍即最后一拍
# 每个返回拍（含首拍）都向上游发一个读请求包
rd_access <= (~ractive_reg & read_active)         # 首拍：read_active 上升沿
          | (rvalid & rready & ~rlast)            # 后续拍：上一拍不是 last
rd_srcaddr <= RETURN_ADDR                         # 告诉求远端把结果回送到哪
rd_dstaddr <= axi_araddr
# emesh2packet 拼成 rd_packet, write 位恒 0
```

关键设计：`rd_srcaddr` 不是源地址，而是**回信地址**（`RETURN_ADDR`）。它告诉远端「请把读结果发回 `0x810D0000`」，这个地址会被本侧 elink 的 RX 路由进 rxrr 通道，最终回到 esaxi 的 `rr_*` 输入。详见 4.2。

**③ 读响应组装（emesh rxrr → AXI R）**

```
rr_wait = 0                                       # 从不向上游反压
on rr_return_access:
    s_axi_rvalid <= 1
    s_axi_rresp  <= 超时? 2'b10(SLVERR) : 2'b00(OKAY)
    按 arsize 广播 rr_data:
        size=0(字节):  rdata <= {4{data[7:0]}}
        size=1(半字):  rdata <= {2{data[15:0]}}
        size=2(字):    rdata <= data[31:0]
on (rready & ~rr_return_access):  s_axi_rvalid <= 0
```

注意 esaxi 把子字**广播复制**到 32 位 R 通道（与 emaxi 的「右对齐抽取」相反），这是因为 CPU 侧 emesh 习惯右对齐、而 AXI slave 这里选择广播策略。

**④ 超时兜底**

由于「elink 读通常不保序」，esaxi **只允许一个未完成读**（注释明说）。若某个读请求发出去后迟迟等不到 rxrr 回信，不能让 R 通道永远卡死。于是有一台状态机：

```
IDLE --(arvalid&arready)--> ARMED   # 装填计数器 = 全 1
ARMED: 每拍计数器 -1
       |-- rr_access ------> IDLE    # 正常回来，清零
       |-- 计数器到 0 ------> EXPIRED
EXPIRED --> IDLE                     # 注入一拍 DEADBEEF + SLVERR 后释放总线
```

计数器位宽 `TW=16`，从全 1 递减到 0，故超时阈值约为 \(2^{16}=65536\) 个 `s_axi_aclk` 周期。超时时返回 `32'hDEADBEEF` 与 `rresp=2'b10`（AXI 的 SLVERR/Exclusive OK 第二位为 1 表错误），让总线不至于挂死。

#### 4.1.3 源码精读

**端口与参数**：[axi/hdl/esaxi.v:17-27](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L17-L27) 定义 `RETURN_ADDR`（回信地址，默认 0）、`AW=32`、`DW=32` 与超时计数器位宽 `TW=16`。

**三条 emesh 通道端口**：写请求 [axi/hdl/esaxi.v:32-34](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L32-L34)、读请求 [axi/hdl/esaxi.v:39-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L39-L41)、读响应 [axi/hdl/esaxi.v:46-48](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L46-L48)。注意写/读请求是 esaxi **输出**、其 `wait` 是 esaxi **输入**（被 elink TX 反压）；读响应 `rr_access/rr_packet` 是 esaxi **输入**、`rr_wait` 是 esaxi **输出**。

**字段 ⇄ 包的三个例化**（核心装配点）：

- 写请求打包，`write` 恒为 1、`srcaddr` 恒为 0（仅支持 32 位 slave 写）：[axi/hdl/esaxi.v:169-179](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L169-L179)
- 读请求打包，`write` 恒为 0、`srcaddr` 取回信地址：[axi/hdl/esaxi.v:182-192](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L182-L192)
- 读响应解包，只消费 `data` 字段：[axi/hdl/esaxi.v:194-204](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L194-L204)

```verilog
emesh2packet e2p_rd (
   .packet_out (rd_packet[PW-1:0]),
   .write_out  (1'b0),                 // 读请求
   .datamode_out (rd_datamode[1:0]),
   .dstaddr_out  (rd_dstaddr[AW-1:0]),
   .data_out     (32'b0),
   .srcaddr_out  (rd_srcaddr[AW-1:0])); // = RETURN_ADDR，回信地址
```

> 这正是 u8-l1 提到的接口漂移点：`emesh2packet`/`packet2emesh` 在全仓库无定义。可用 `grep -rn "module emesh2packet\|module packet2emesh" .` 自行验证（当前返回空）。

**写地址通道状态机**：[axi/hdl/esaxi.v:214-238](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L214-L238) 维护 `s_axi_awready` 与 `write_active`——空闲时持续拉高 `awready`（注释里还留了一句「TODO: why not set default as 1?」，说明作者也注意到 AXI 推荐默认高），抓到地址后拉低直到本次写结束。地址/size/burst/id 的捕获在 [axi/hdl/esaxi.v:240-264](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L240-L264)，其中突发自增分支带一条醒目的作者留言「TODO FIX This, this is not right (double bug canceling!!)」，提示写突发地址自增逻辑尚未定稿，读源码时须留意。

**写响应 B 通道**：[axi/hdl/esaxi.v:271-302](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L271-L302)——在 `last_wr_beat`（[L210](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L210-L210)）那一拍拉高 `bvalid`、`bresp=2'b0`（OKAY）。`bid` 直接回传捕获的 `awid`（[L269](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L269-L269)），让 master 能配对。

**写数据对齐 + wr_access 流水**：[axi/hdl/esaxi.v:372-416](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L372-L416)。核心两件事：

```verilog
pre_wr_en   <= s_axi_wready & s_axi_wvalid;   // 当拍握手
wr_access   <= pre_wr_en;                      // 下一拍出包（对齐）
// 按 wstrb 选字节车道，并据此刻填 wr_dstaddr[1:0]
if(s_axi_wstrb[0]) begin wr_data_reg <= s_axi_wdata[31:0];   wr_dstaddr_reg[1:0]<=2'd0; end
else if(s_axi_wstrb[1]) begin wr_data_reg <= {8'd0,s_axi_wdata[31:8]};  wr_dstaddr_reg[1:0]<=2'd1; end
...
```

即 AXI 的 `wstrb`（字节使能）被翻译成 emesh 的「子字地址低 2 位 + 数据左对齐」，让远端能正确写入单个字节/半字。`wr_datamode` 取自 `axi_awsize[1:0]`（[L385](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L385-L385)），数值上 AXI size 与 emesh datamode 相等（0/1/2 = 字节/半字/字）。

**读请求 AR 通道**：[axi/hdl/esaxi.v:311-365](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L311-L365)。其中 [L351](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L351-L351) 用 `s_axi_rlast <= ~(|s_axi_arlen)` 预判「首拍是否即末拍」（单拍读时 arlen=0），突发读则在 [L354-364](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L354-L364) 逐拍递减 `arlen`、自增地址。`rd_access` 的生成在 [axi/hdl/esaxi.v:430-449](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L430-L449)：

```verilog
ractive_reg <= read_active;
rnext       <= s_axi_rvalid & s_axi_rready & ~s_axi_rlast;
rd_access   <= (~ractive_reg & read_active) | rnext;  // 首拍 | 后续拍
rd_srcaddr  <= RETURN_ADDR;                            // 回信地址
```

**读响应组装**：[axi/hdl/esaxi.v:457-485](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L457-L485)。`rr_wait = 1'b0`（[L457](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L457-L457)，永不反压 rxrr）；数据按 size 广播：

```verilog
case(axi_arsize[1:0])
  2'b00:   s_axi_rdata <= {4{rr_return_data[7:0]}};   // 字节广播
  2'b01:   s_axi_rdata <= {2{rr_return_data[15:0]}};  // 半字广播
  default: s_axi_rdata <= rr_return_data[31:0];       // 字
endcase
```

**超时电路**：状态机 [axi/hdl/esaxi.v:496-508](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L496-L508)、计数器 [L511-517](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L511-L517)。`rr_return_data` 与 `rr_return_access` 在 [L460-462](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L460-L462) 选择「真数据」还是「DEADBEEF」：

```verilog
assign rr_return_access = rr_access | rr_timeout_access;
assign rr_return_data   = rr_timeout_access ? 32'hDEADBEEF : rr_data[31:0];
// rresp:  rr_timeout_access ? 2'b10 : 2'b00   (见 L476)
```

#### 4.1.4 代码实践

**实践目标**：用纯阅读方式，跟踪一次 esaxi 的「读—返回—超时」全过程，理解读响应是怎么被组装出来的。

**操作步骤（源码阅读型）**：

1. 打开 [axi/hdl/esaxi.v:430-449](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L430-L449)，确认一次 AXI 单拍读（`arlen=0`）只会在 `read_active` 上升沿那一拍产生**一个** `rd_access` 脉冲。
2. 跟随 `rd_srcaddr <= RETURN_ADDR`，记住这个回信地址会被远端塞进响应包的 `dstaddr` 字段。
3. 跳到读响应段 [axi/hdl/esaxi.v:464-485](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L464-L485)，确认 `rr_access` 一来就拉高 `s_axi_rvalid`，数据按 `arsize` 广播。
4. 再看超时段 [axi/hdl/esaxi.v:496-518](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L496-L518)，理解若 `rr_access` 在计数器归零前不来，则 `rr_timeout_access` 注入一拍 `DEADBEEF + SLVERR`。

**需要观察的现象**：在脑中画一张时序图，包含 `arvalid/arready`、`rd_access`、`rr_access`、`s_axi_rvalid`、以及超时分支 `rr_timeout_access` 五条线。

**预期结果**：正常返回时 `rr_access` 与 `s_axi_rvalid` 几乎同拍（组合上多一级寄存器），`rresp=00`；超时时 `rdata=DEADBEEF`、`rresp=10`，且超时只持续一拍即回到 IDLE。

> 因 `packet2emesh`/`emesh2packet` 无定义，本实践**无法在 iverilog 中直接跑通**，属「源码阅读型实践」，结论待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 esaxi 的 `rr_wait` 恒为 0、而对 `wr_wait/rd_wait` 却要老老实实接 elink 的反压？

**参考答案**：`wr_wait/rd_wait` 来自 elink 的 TX 通道，当链路忙或对端反压时必须停发新包，否则丢包；而 `rr_wait` 是 esaxi **向 elink RX 发出的**反压，esaxi 一侧只要 `s_axi_rready` 配合即可消化返回数据，设计上选择「永不反压上游 rxrr」，故恒 0。

**练习 2**：esaxi 把读回的子字「广播复制」到 32 位 R 通道，而 emaxi 却是「右对齐抽取」。这两种策略各有什么前提？

**参考答案**：广播要求接收方（CPU）自己按 `arsize`/地址低位从正确字节车道取数，电路简单但总线位数浪费；右对齐要求桥自身根据地址把目标子字搬到低位，接收方直接读低位即可。二者都是把窄数据塞进宽总线的合法手段，区别在于「谁负责对齐」。

**练习 3**：超时阈值约为多少个时钟周期？为何要兜这一层？

**参考答案**：`TW=16`，计数器从全 1 递减到 0，阈值约 \(2^{16}=65536\) 个 `s_axi_aclk` 周期。兜底是因为 esaxi「只允许一个未完成读」，若远端永远不回信，R 通道会无限期占用、阻塞后续所有读，超时注入一拍错误响应可释放总线。

---

### 4.2 axi_elink 顶层集成

#### 4.2.1 概念说明

`axi_elink` 是把 elink 芯片做成「**一颗带 AXI 接口的桥 SoC**」的顶层包装。它在内部同时实例化三个大模块：

- **elink**：第 7 单元讲的高速 LVDS 链路核，负责把 104 位 emesh 包串行化送上差分线、以及把线上比特还原成 emesh 包；
- **esaxi**：4.1 讲的 AXI **从**桥（本地 AXI master → 链路）；
- **emaxi**：u8-l1 讲的 AXI **主**桥（链路 → 本地 AXI slave，如 DDR）。

三者的关系用一句话概括：**elink 是「网线」，esaxi 是「CPU 侧网卡」，emaxi 是「内存侧网卡」**。CPU 写 esaxi 的 S_AXI，数据经 elink 发到远端；远端发来的访问经 elink 到 emaxi，由 emaxi 驱动 M_AXI 读写本地 DDR。

`axi_elink` 顶层本身几乎不含逻辑，它的全部价值在于**那一组精心设计的内部 net 与 AUTO_TEMPLATE 重命名**——把三个模块的六条 emesh 通道（txwr/txrd/txrr/rxwr/rxrd/rxrr）按「谁产生、谁消费」精确对接。

#### 4.2.2 核心流程

先看顶层的三模块与六通道对接图（`↔` 为 emesh 通道，方向按「数据流向」标注）：

```
                  ┌────────────── axi_elink 顶层 ──────────────┐
  本地 CPU ──S_AXI──▶ esaxi ──txwr/txrd──▶ ┌──────────┐
                  ◀──      ◀──rxrr───  ─── │          │ ──► txo_data_p/n  ──► 远端
                  │                        │  elink   │
  本地 DDR ◀─M_AXI── emaxi ◀──rxwr/rxrd─── │          │ ◀── rxi_data_p/n  ◀── 远端
                  ▶──      ──txrr──▶  ──── └──────────┘
                  └────────────────────────────────────────────┘
```

对接规则（看图即懂，记不住就回来查表）：

| emesh 通道 | 产生方（access/packet） | 消费方（wait） | 物理含义 |
|-----------|------------------------|---------------|----------|
| `txwr` 写请求 | esaxi | elink | CPU 的写送上链路 |
| `txrd` 读请求 | esaxi | elink | CPU 的读请求送上链路 |
| `txrr` 读响应 | emaxi | elink | 远端读请求的**结果**送上链路回远端 |
| `rxwr` 写请求 | elink | emaxi | 远端的写送到本地 DDR |
| `rxrd` 读请求 | elink | emaxi | 远端的读请求送到本地 DDR |
| `rxrr` 读响应 | elink | esaxi | 远端把 CPU 读的结果送回 |

可见 **TX 侧由 esaxi 主导写/读请求、emaxi 主导读响应；RX 侧由 emaxi 主导写/读请求、esaxi 消费读响应**。这正是 esaxi/emaxi 镜像对称的体现。

**端到端事务流（以本地 CPU 写远端为例）**：

1. CPU 在 S_AXI 上发 AW + W；
2. esaxi 把它打包成 emesh 写包，拉 `txwr_access`；
3. elink 的 etx 通路仲裁（txwr/txrd/txrr）→ 成帧 → DDR 串化 → `txo_data_p/n`、`txo_frame_p/n`、`txo_lclk_p/n` 送出（u7-l2）；
4. 远端芯片的 erx 还原成包，执行写；
5. 写是「即发即忘」，无返回（esaxi 在 `last_wr_beat` 那拍直接回 `bresp=OKAY`，不等远端确认）。

**端到端事务流（以本地 CPU 读远端为例）**：

1. CPU 在 S_AXI 上发 AR；
2. esaxi 打包成读请求，`srcaddr = RETURN_ADDR = 0x810D0000`，拉 `txrd_access`；
3. elink etx 把读请求串化送上链路；
4. 远端执行读，把结果包的 `dstaddr` 设为收到的 `srcaddr`（即 `0x810D0000`）发回；
5. 本地 elink erx 收到，按 `dstaddr[31:20]=0x810`（本芯片 ID）+ `dstaddr[19:16]=0xD`（`EGROUP_RR`）路由进 **rxrr** 通道；
6. esaxi 的 `rr_access/rr_packet` 收到，组装成 AXI R 通道数据交还 CPU（若超时则回 `DEADBEEF`）。

这里的精妙之处：**回信地址的低 4 位组号 `EGROUP_RR` 就是把响应「喂」进 rxrr FIFO 的路由钥匙**（[elink/hdl/elink_regmap.vh:17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L17-L17)）。

#### 4.2.3 源码精读

**顶层参数与回信地址**：[elink/hdl/axi_elink.v:37-45](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L37-L45) 定义 `AW=32`、`PW=2*AW+40`、`ID=12'h810`（地址 `[31:20]` 的本芯片号）、`S_IDW=12`（从端口 ID 宽）、`M_IDW=6`（主端口 ID 宽），以及回信地址：

```verilog
parameter ID = 12'h810;                 // addr[31:20] id
parameter RETURN_ADDR = {ID, `EGROUP_RR, 16'b0};  // = 0x810D0000
```

`EGROUP_RR = 4'hD` 来自 [elink/hdl/elink_regmap.vh:17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L17-L17)（`// read response block`）。把这个 `RETURN_ADDR` 作为 esaxi 的读请求 `srcaddr`，就把远端响应精确地路由回了 rxrr。

**内部 net（AUTOWIRE）**：[elink/hdl/axi_elink.v:164-184](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L164-L184) 声明了六通道的全部内部连线，注释标明了每根线的「来源」——例如 `wire txwr_access; // From esaxi`、`wire rxwr_wait; // From emaxi`。这是理解顶层接线的「地图」。

**elink 实例化**：[elink/hdl/axi_elink.v:192-243](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L192-L243)。把物理 IO（`rxi_*`/`txo_*`/`cclk`/`chipid` 等）与六通道 emesh 全部引到内部 net。注意 `sys_clk` 同时作为 elink 的系统时钟。

**esaxi 实例化与 AUTO_TEMPLATE 改名**：[elink/hdl/axi_elink.v:249-309](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L249-L309)。这段 verilog-mode 模板是顶层集成的「核心机密」：

```verilog
/*esaxi AUTO_TEMPLATE (
   .rr_\(.*\)   (rxrr_\1[]),   // esaxi 的 rr_*  ↔ rxrr_*（读响应来自 RX RR）
   .rd_\(.*\)   (txrd_\1[]),   // esaxi 的 rd_*  ↔ txrd_*（读请求送上 TX）
   .wr_\(.*\)   (txwr_\1[]));  // esaxi 的 wr_*  ↔ txwr_*（写请求送上 TX）
*/
esaxi #(.S_IDW(S_IDW), .RETURN_ADDR(RETURN_ADDR)) esaxi (
   .s_axi_aclk (sys_clk), ...
   .wr_access (txwr_access), .wr_packet (txwr_packet[PW-1:0]),
   .rd_access (txrd_access), .rd_packet (txrd_packet[PW-1:0]),
   .rr_wait   (rxrr_wait),   .rr_access (rxrr_access), .rr_packet (rxrr_packet[PW-1:0]),
   ...);
```

> 小贴士（承接 u4-l3）：`AUTO_TEMPLATE` 是 emacs verilog-mode 的展开指令，iverilog 并不识别；仓库里的 `.v` 文件已是展开后的最终接线结果，所以你直接读下面的 `esaxi (...)` 实例化正文即可，模板注释只是「生成依据」。

**emaxi 实例化与 AUTO_TEMPLATE 改名**：[elink/hdl/axi_elink.v:314-372](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L314-L372)。emaxi 的改名规则与 esaxi **互补**：

```verilog
/*emaxi AUTO_TEMPLATE (
   .rr_\(.*\)   (txrr_\1[]),   // emaxi 的 rr_* ↔ txrr_*（读响应送上 TX 回远端）
   .rd_\(.*\)   (rxrd_\1[]),   // emaxi 的 rd_* ↔ rxrd_*（远端读请求来自 RX）
   .wr_\(.*\)   (rxwr_\1[]));  // emaxi 的 wr_* ↔ rxwr_*（远端写请求来自 RX）
*/
```

把 4.2.2 的对接表与这两段模板对照，你会发现每一行都严丝合缝：esaxi 的 `wr` 走 TX、emaxi 的 `wr` 走 RX；二者的 `rr` 一个进 RXRR、一个出 TXRR。这就是「镜像集成」的全部秘密。

**时钟域**：两个 AXI 桥都用 `sys_clk` 作 `s_axi_aclk`/`m_axi_aclk`（[L258](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L258-L258)、[L321](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L321-L321)）。elink 内部再用 FIFO 把 `sys_clk` 域与 LVDS 的 `tx_lclk/rx_lclk` 域隔开（u7-l2/u7-l3），所以 AXI 侧只需关心单一 `sys_clk`。

#### 4.2.4 代码实践

**实践目标**：画出「AXI master → axi_elink → elink → 远端 emesh slave」的完整事务通路框图（本讲指定的综合实践，详见第 5 节）。这里先做一个缩小版：在源码里逐段「盖章」确认每一段通路。

**操作步骤（源码阅读型）**：

1. 在 [axi_elink.v:256-309](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L256-L309) 找到 S_AXI 信号进入 esaxi、`txwr_access/txwr_packet` 出来的位置——盖章「① CPU→esaxi→txwr」。
2. 在 [axi_elink.v:213-221](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L213-L221) 找到 `txwr_access/packet` 接入 elink、`txo_data_p/n` 等物理 IO 输出——盖章「② txwr→elink→线上」。
3. 回忆 u7-l2 的 etx 流水线（`etx_arbiter → etx_protocol → etx_io`），把「线上」这一段补全。
4. 对读路径，在 [axi_elink.v:280-281](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L280-L281) 找到 `rxrr_access/packet` 回到 esaxi——盖章「③ 线上→elink rxrr→esaxi→CPU」。

**需要观察的现象**：六通道 net 中，写请求通路（txwr）与读响应通路（rxrr）共用 esaxi，但走的是不同通道、互不阻塞。

**预期结果**：你能用一张图把「S_AXI → esaxi → txwr → elink.etx → txo_data」与「rxi_data → elink.erx → rxrr → esaxi → S_AXI.R」两条链路分别画清。

> 同样因 `packet2emesh`/`emesh2packet` 缺失，整个 `axi_elink` 无法直接编译仿真，属阅读型实践，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RETURN_ADDR` 的 `[19:16]` 要放 `EGROUP_RR` 而不是别的组？

**参考答案**：因为 elink 的 RX 路由器（erx_arbiter）按 `dstaddr` 的组号把包分发到 rxwr/rxrd/rxrr 三个 FIFO。把回信地址的组号设为 `EGROUP_RR`（0xD），远端返回的响应包一进本侧 elink 就被精确送进 rxrr FIFO，从而被正在等它的 esaxi 消费。若设成别的组，响应会错路由到 rxwr/rxrd，CPU 永远读不到结果。

**练习 2**：esaxi 的 S_AXI 数据是 32 位、emaxi 的 M_AXI 数据是 64 位。这种不对称会带来什么后果？

**参考答案**：CPU 侧一次最多写 32 位（4 字节，`wstrb[3:0]`），需要 esaxi 做字节/半字对齐；内存侧一次可写 64 位（8 字节，`wstrb[7:0]`），emaxi 需要把 emesh 的 32 位数据广播/对齐到 64 位总线上。两个桥各自承担自己侧的「窄↔宽」对齐职责，elink 链路上跑的始终是 104 位 emesh 包，与两端位宽无关。

**练习 3**：如果同时存在「CPU 读远端」和「远端读本地 DDR」两个未完成读，会互相干扰吗？

**参考答案**：不会，因为它们走**不同的 emesh 通道**与**不同的 AXI 端口**。CPU 读走 esaxi（txrd 出、rxrr 回）；远端读走 emaxi（rxrd 入、txrr 出）。二者在 elink 内部由独立 FIFO 隔离，AXI 侧也是独立的 S_AXI 与 M_AXI 端口。唯一约束是 esaxi 自身「只允许一个未完成读」（emaxi 则用 FIFO 记账可多个）。

## 5. 综合实践

**任务**：画出本讲指定的「AXI master → axi_elink → elink → 远端 emesh slave」完整事务通路框图，并标注每一段对应的源码位置。

**要求**：

1. 用一张图同时表达**写事务**（即发即忘）与**读事务**（带返回）两条通路；
2. 在图上至少标出这些「关卡」及其源码锚点：
   - ① S_AXI 进入 esaxi：[axi_elink.v:256-309](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L256-L309)
   - ② esaxi 打包出 txwr/txrd：[esaxi.v:169-192](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L169-L192)
   - ③ 六通道进 elink：[axi_elink.v:192-243](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L192-L243)
   - ④ elink etx 串化上网线：[elink/README.md:137-158](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L137-L158)（设计结构图）
   - ⑤ 读响应经 rxrr 回到 esaxi：[axi_elink.v:280-281](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L280-L281) 与 [esaxi.v:194-204](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/esaxi.v#L194-L204)
   - ⑥ 回信地址 `RETURN_ADDR=0x810D0000` 的路由钥匙：[elink_regmap.vh:17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh#L17-L17)
3. 用不同颜色/线型区分「写请求」「读请求」「读响应」三种数据流。

**进阶（可选）**：在图上再叠一层「远端→本地」方向，画出远端 emesh 请求如何经 elink RX 到达 emaxi 的 M_AXI（对照 [axi_elink.v:320-372](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/axi_elink.v#L320-L372)），体会 esaxi/emaxi 的镜像对称。

**预期结果**：一张能独立向同学讲明白「CPU 一条读写指令，是如何跨越 LVDS 链路抵达远端芯片」的框图。

> 本实践为纯设计/阅读型，无需运行工具；若要仿真验证，需先补齐 `packet2emesh`/`emesh2packet` 定义或借助平台库替换，属后续工程化工作。

## 6. 本讲小结

- `esaxi` 是 AXI **从**桥：把本地 CPU 的 S_AXI 写/读翻译成 emesh 包送上 elink 的 txwr/txrd 通道，并把远端经 rxrr 通道返回的读响应组装回 AXI 的 R 通道（数据按 size 广播）。
- esaxi 的写响应在 `last_wr_beat` 即回 `OKAY`（即发即忘）；读响应带一套约 \(2^{16}\) 周期的超时状态机，超时返回 `DEADBEEF + SLVERR`，且「只允许一个未完成读」。
- `axi_elink` 是纯集成顶层：用一组内部 net + 两段互补的 `AUTO_TEMPLATE`，把 `esaxi`（wr→txwr、rd→txrd、rr↔rxrr）与 `emaxi`（wr↔rxwr、rd↔rxrd、rr→txrr）镜像对接到 `elink` 的六通道。
- esaxi 与 emaxi 是镜像关系：一个 AXI-slave/emesh-TX，一个 emesh-RX/AXI-master；数据位宽也对称地取 32 位（CPU 侧）与 64 位（DDR 侧）。
- 读请求的 `srcaddr = RETURN_ADDR = 0x810D0000` 是路由钥匙：其组号 `EGROUP_RR`（0xD）保证远端响应被本侧 elink 精确送进 rxrr FIFO、回到 esaxi。
- 工程现实：`packet2emesh`/`emesh2packet` 在仓库中无定义，`esaxi`/`emaxi`/`axi_elink` 均不能原样编译，本讲结论以源码文本与 README 为准、关键行为待本地仿真确认。

## 7. 下一步学习建议

- **u8-l3（edma DMA 引擎）**：从「单包桥接」上升到「批量搬运」，看 edma 如何在 emesh 接口上跑 1D/2D stride 与描述符链，会复用本讲建立的事务流直觉。
- **重读 u7-l2/u7-l3**：本讲把 elink 当「黑盒网线」，建议回头对照 etx/erx 流水线，把「③→④」这一段在物理层是如何逐比特串化/解串的补全。
- **动手（进阶）**：尝试为 `packet2emesh`/`emesh2packet` 写一份最小行为模型（参考 u5-l1 的字段表与 u5-l3 的 pack/unpack），让 `esaxi` 能在 iverilog 里至少跑通「单拍写 + 单拍读」的回环，验证本讲的端到端通路结论。
