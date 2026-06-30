# axi_lite_to_apb：把 AXI4-Lite 桥接到 APB4

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 APB4 总线的信号集与「Setup / Access 两拍访问」时序。
- 看懂 `axi_lite_to_apb` 如何把 AXI4-Lite 的读、写请求拆成统一的内部请求，并在两者之间仲裁。
- 复述 APB 主端状态机（`Setup` → `Access`）如何产生 `psel/penable` 两相波形、如何借助 `addr_decode` 选片。
- 解释 AXI-Lite 与 APB 之间的字段映射规则，尤其是地址对齐与 `pslverr → resp` 的错误码翻译。
- 理解可选流水线寄存器（`PipelineRequest` / `PipelineResponse`）与接口外壳 `axi_lite_to_apb_intf` 的作用。
- 能够在 `tb_axi_lite_to_apb` 的基础上，自己接一个简易 APB 从端模型并验证两相时序。

## 2. 前置知识

本讲假设你已经掌握：

- **AXI4-Lite 的通道结构**（AW/W/B/AR/R，无 burst、无 id、无 atop），见讲义 u12-l1。
- **valid/ready 握手**与「在途 / pending」概念，见讲义 u1-l3、u2-l3。
- **`req_t` / `resp_t` 结构体 + `AXI_LITE_TYPEDEF` / `AXI_LITE_ASSIGN` 宏体系**，见讲义 u2-l4、u12-l1。
- **`rr_arb_tree`、`spill_register`、`fall_through_register`、`addr_decode`** 这几个来自外部依赖 `common_cells` 的通用原语（讲义 u4-l1、u5-l1 已多次提及）。
- **`axi_pkg::RESP_*` 响应码**：`RESP_OKAY=2'b00`、`RESP_SLVERR=2'b10`、`RESP_DECERR=2'b11`（见 u1-l3、u2-l1）。

关于 APB，你**不需要**任何先验知识——本讲会从零讲起。只需记住一句话：APB 是 ARM AMBA 家族里最简单的外设总线，没有 valid/ready 双向握手，而是一种「主端发起两拍、从端用 ready 拉长」的固定节拍协议。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/axi_lite_to_apb.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv) | 模块本体，含结构体内核 `axi_lite_to_apb` 与接口外壳 `axi_lite_to_apb_intf` 两个 module。是全库唯一的 APB 模块，位于编译层级 **Level 2**。 |
| [test/tb_axi_lite_to_apb.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv) | 测试台：用 `axi_lite_rand_master` 发随机 Lite 事务，APB 从端用 `$urandom` 每拍随机更新响应，并用 `assert property` 检查 APB 协议时序。 |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 提供 `aligned_addr`、`RESP_*`、`prot_t` 等被本模块复用的类型与函数（Level 0 根包）。 |

> 提示：本库用 Bender 管理依赖，`axi_lite_to_apb` 位于 [Bender.yml](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml) 的 **Level 2** 段（与 `axi_cut`、`axi_lite_regs` 同层），说明它只依赖 Level 0–1 的根基文件与 `common_cells` 原语，本身不依赖任何 xbar / mux。

---

## 4. 核心概念与源码讲解

### 4.1 APB4 协议入门：两拍访问与信号集

#### 4.1.1 概念说明

APB（Advanced Peripheral Bus）是 AMBA 家族中面向**低带宽、低复杂度外设**（定时器、UART、GPIO 配置寄存器等）的总线。本模块面向的是 **APB4**（含 `PREADY`、`PSLVERR`、`PSTRB`、`PPROT` 的版本），它与 AXI4-Lite 一样没有突发、没有 ID，但握手模型完全不同：

- AXI4-Lite 用 **valid/ready 双向握手**，任意一拍可独立完成。
- APB4 用 **主端驱动的两拍节拍**：主端先发「Setup」，下一拍发「Access」，从端只能在 Access 拍通过 `PREADY` 决定「这次完成」还是「再等一拍」。

之所以需要桥接，是因为片上高性能总线（AXI）和慢速外设（APB）经常共存：CPU 走 AXI4-Lite 访问寄存器块，而很多老 IP 只有 APB 口。`axi_lite_to_apb` 就是这两套握手之间的「翻译官」。

#### 4.1.2 核心流程

APB4 一次访问的信号集与两拍时序如下：

```
PCLK   ─┐  ┌─┐  ┌─┐  ┌─┐  ┌─┐  ┌─┐
        └──┘ └──┘ └──┘ └──┘ └──┘ └──
PSELx     0    1    1    1    0       (选中目标从端)
PENABLE   0    0    1    1    0       (0=Setup, 1=Access)
PADDR     -   A0   A0   A0    -       (地址在 Setup 给出后保持)
PWRITE    -   rw   rw   rw    -
PWDATA    -   D0   D0   D0    -       (写数据)
PREADY    x    x    0    1    x       (从端: 0=等待, 1=完成)
PRDATA    x    x    x    R0   x       (读数据, Access 拍有效)
PSLVERR   x    x    x    err  x       (错误指示, Access 拍有效)
         idle  Setup Access          idle
              (传输)
```

关键规则：

1. **Setup 拍**：`PSELx=1, PENABLE=0`，主端驱动地址 / 写使能 / 写数据。
2. **Access 拍**：`PSELx=1, PENABLE=1`，从端用 `PREADY` 应答。
   - `PREADY=1`：本拍完成，读数据 `PRDATA` 与错误位 `PSLVERR` 在此拍有效，下一拍可发起新 Setup。
   - `PREADY=0`：插入等待态，Access 拍延续，地址 / 控制信号必须**保持稳定**。
3. **`PSLVERR`** 仅在 Access 且 `PREADY=1` 时有意义，为 1 表示从端出错。

本模块的源码头注释把上述信号集与结构体字段一一列了出来，见 [src/axi_lite_to_apb.sv:25-46](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L25-L46)。

#### 4.1.3 源码精读

模块对 `apb_req_t` / `apb_resp_t` 两个结构体的「契约」写在了文件头注释里——这是阅读本模块最重要的一段说明：每个 APB 从端一份 `apb_req_t`，其中只有被地址译码选中的那一份 `psel=1`，其余字段的值允许是未定义（`x`）：

[src/axi_lite_to_apb.sv:25-46](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L25-L46)

```systemverilog
//   typedef struct packed {
//     addr_t paddr;   // same as AXI4-Lite
//     prot_t pprot;   // same as AXI4-Lite, specification is the same
//     logic  psel;    // each APB4 slave has its own single-bit psel
//     logic  penable; // enable signal shows second APB4 cycle
//     logic  pwrite;  // write enable
//     data_t pwdata;  // write data, comes from W channel
//     strb_t pstrb;   // write strb, comes from W channel
//   } apb_req_t;
// ...
//  typedef struct packed {
//    logic  pready;   // slave signals that it is ready
//    data_t prdata;   // read data, connects to R channel
//    logic  pslverr;  // gets translated into either `axi_pkg::RESP_OK` or `axi_pkg::RESP_SLVERR`
//  } apb_resp_t;
```

注意 `psel` 是 **每个 APB 从端一位**（而不是一个 one-hot 总线），所以端口是数组 `apb_req_t [NoApbSlaves-1:0]`。这种「每从端一份请求结构体」的设计让每个从端都收到自己的独立请求，互不干扰。

> 关于 AXI4-Lite 与 APB 的 `prot`：注释明确指出两者 `pprot` 的位定义**完全一致**（都是 3 位的特权 / 安全 / 指令位），所以可以原样搬运，无需翻译。

#### 4.1.4 代码实践

**实践目标**：在没有仿真器的情况下，凭协议画出一次「读 + 等待态」的 APB 时序。

**操作步骤**：
1. 假设主端在 `T2` 发起一次读（`PADDR=0x4000, PWRITE=0`），从端在第一个 Access 拍不 ready、第二个 Access 拍才 ready。
2. 在纸上画出 `PSELx / PENABLE / PADDR / PREADY / PRDATA / PSLVERR` 在 `T2(setup) / T3(access) / T4(access) / T5(idle)` 四拍的取值。

**需要观察的现象**：Access 拍延续期间，`PADDR`、`PWRITE` 必须保持不变；只有 `PREADY=1` 的那一拍 `PRDATA` 才有效。

**预期结果**：`T2`: psel=1,penable=0；`T3`: psel=1,penable=1,pready=0（等待）；`T4`: psel=1,penable=1,pready=1,prdata=R0（完成）；`T5`: psel=0。这正好对应 [tb_axi_lite_to_apb.sv:176-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv#L176-L193) 里定义的 `APB_IDLE / APB_SETUP / APB_ACCESS / APB_TRANSFER` 四个序列。

#### 4.1.5 小练习与答案

**练习 1**：APB4 相比 APB3 多了哪两个关键信号？它们分别解决什么问题？

> **答案**：多了 `PREADY` 和 `PSLVERR`。`PREADY` 让从端可以插入等待态（APB3 假设从端总是固定两拍完成）；`PSLVERR` 让从端能向上报错（APB3 无错误指示）。

**练习 2**：为什么 `PENABLE` 必须在 Setup 拍为 0、Access 拍为 1，而不能省略？

> **答案**：APB 没有双向握手，从端需要一根明确的状态线来区分「地址刚给出、请锁存」与「数据相、请应答」。`PENABLE` 的 0→1 跳变就是这个节拍指示。

---

### 4.2 AXI-Lite 端口拆解：统一内部请求与读写仲裁

#### 4.2.1 概念说明

AXI4-Lite 有读、写两类事务，走不同的通道（AR/R 与 AW/W/B）。但 APB 主端 FSM 一次只能服务一笔事务，且不分读写地输出同一组 `P*` 信号。因此第一步必须把 Lite 的读、写请求**归一化成同一种内部格式**，然后**仲裁**出当前要送进 APB FSM 的那一笔。

这里体现的是讲义 u2-l4 反复强调的范式：「接口外壳 + 结构体内核」。模块对外的 AXI-Lite 是结构体 `axi_lite_req_t` / `axi_lite_resp_t`（见 [端口列表 L66-L67](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L66-L67)），内核把它们拆解后重新打包成内部类型。

#### 4.2.2 核心流程

归一化的核心是一个内部请求结构体 `int_req_t`，它把读和写合并到同一形状：

```
AXI-Lite 读:  AR.addr, AR.prot  ──┐
                                   ├─► int_req_t { addr, prot, data, strb, write }
AXI-Lite 写:  AW.addr, AW.prot,    │      (write 位区分读=0 / 写=1)
              W.data, W.strb  ────┘
                                   │
                                   ▼
                          rr_arb_tree (NumIn=2)
                                   │   (读、写两路轮询仲裁, LockIn=1)
                                   ▼
                          apb_req  (送入 APB FSM)
```

两路归一化请求被送进一棵 `rr_arb_tree` 做轮询仲裁：每个时钟沿最多选读或写中的一笔往下传。由于 APB 是串行节拍协议，读、写不可能同时发起，所以这里必须串行化。

#### 4.2.3 源码精读

先看内部类型定义。`int_req_t` 用一个 `write` 位区分读 / 写，读请求的 `data/strb` 填 0；`int_resp_t` 把 APB 的读数据与翻译后的响应码打包在一起。`apb_state_e` 定义了 FSM 的两态：

[src/axi_lite_to_apb.sv:82-96](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L82-L96)

```systemverilog
typedef struct packed {
  addr_t          addr;
  axi_pkg::prot_t prot;
  data_t          data;
  strb_t          strb;
  logic           write;
} int_req_t;
typedef struct packed {
  data_t          data; // read data from APB
  axi_pkg::resp_t resp; // response bit from APB
} int_resp_t;
typedef enum logic {
  Setup  = 1'b0, // APB in Idle or Setup
  Access = 1'b1  // APB in Access
} apb_state_e;
```

接着看把 AXI-Lite 拆成两路内部请求的组合逻辑。读请求（`RD=0`）直接由 AR 通道构造；写请求（`WR=1`）的 valid 必须 `aw_valid & w_valid` 同时成立——这是 AXI 写事务的固有要求（地址和数据要齐才能算一笔完整写）：

[src/axi_lite_to_apb.sv:113-129](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L113-L129)

```systemverilog
assign axi_req[RD] = '{
  addr:  axi_lite_req_i.ar.addr,  prot:  axi_lite_req_i.ar.prot,
  data:  '0,  strb:  '0,  write: RD
};
assign axi_req_valid[RD] = axi_lite_req_i.ar_valid;
assign axi_req[WR] = '{
  addr:  axi_lite_req_i.aw.addr,  prot:  axi_lite_req_i.aw.prot,
  data:  axi_lite_req_i.w.data,   strb:  axi_lite_req_i.w.strb,
  write: WR
};
assign axi_req_valid[WR] = axi_lite_req_i.aw_valid & axi_lite_req_i.w_valid;
```

仲裁用的是讲义 u5-l1 出现过的 `rr_arb_tree`，配置 `NumIn=2`、`LockIn=1`（选中后锁定到握手完成）、`AxiVldRdy=1`（遵循 valid/ready 语义）。它把 `axi_req[1:0]` 选一路输出为 `arb_req`：

[src/axi_lite_to_apb.sv:149-167](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L149-L167)。

仲裁结果经一个可选流水线寄存器（见 4.4 节）后变成 `apb_req`，进入 FSM。

AXI-Lite 响应端（B/R）则反向组合：B 响应来自写响应寄存器，R 响应来自读响应寄存器；`aw_ready/w_ready` 都等于「写请求 valid 且被仲裁器接受」：

[src/axi_lite_to_apb.sv:130-138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L130-L138)。

#### 4.2.4 代码实践

**实践目标**：确认写请求必须 AW、W「同时」valid 才会被仲裁。

**操作步骤**：
1. 打开 [src/axi_lite_to_apb.sv:113-138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L113-L138)。
2. 假设上游 master 已经拉高 `aw_valid` 但 `w_valid` 仍为 0，回答：此时 `aw_ready` 可能是 1 吗？为什么？

**需要观察的现象**：`axi_req_valid[WR]` 这一拍为 0，仲裁器看不到写请求。

**预期结果**：`aw_ready = axi_req_valid[WR] & axi_req_ready[WR] = 0`，即写地址通道暂时不会被接受。只有当 `w_valid` 也拉高，写请求才「成形」并可能被接受。这正是 AXI 写事务 AW/W 配对的体现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `int_req_t` 要用一个 `write` 位，而不是像 AXI 那样保持读、写两套独立通道？

> **答案**：因为 APB 只有一组 `P*` 信号、FSM 一次只处理一笔事务，下游是串行的。把读、写归一成同一种格式后，只需一棵 2 选 1 的 `rr_arb_tree` 就能串行化，FSM 也只需一份逻辑。

**练习 2**：`rr_arb_tree` 的 `LockIn=1` 在这里起什么作用？

> **答案**：`LockIn=1` 保证一旦选中某一路（例如一笔写），在它的握手完成前不会切换到另一路（读），避免半截事务被打断。这与讲义 u5-l1 中 demux/mux 使用 `LockIn` 的动机一致。

---

### 4.3 APB 主端两相状态机：Setup → Access 与地址译码

#### 4.3.1 概念说明

把一笔 `int_req_t` 翻译成符合 APB4 协议的两拍波形，靠一个两态有限状态机：`Setup`（含空闲）和 `Access`。FSM 在 `Setup` 拍给出 `psel=1, penable=0` 并用 `addr_decode` 选片，下一拍进 `Access` 给出 `penable=1`，等从端 `pready` 后完成。

这里还要解决「多个 APB 从端怎么选」的问题。模块用 `common_cells` 的 `addr_decode` 把请求地址译码成一个从端索引 `apb_sel_idx`，再用这个索引点亮对应那一槽的 `apb_req_o`。这和讲义 u6-l2 中 xbar 的地址译码思路同源。

#### 4.3.2 核心流程

FSM 的状态转移（伪代码）：

```
state = Setup:
  if 有合法请求 且 响应寄存器就绪:
    addr_decode(addr) -> sel_idx, dec_valid
    if dec_valid 且 (写且有写选通位  或  读):
        apb_req_o[sel_idx] = { psel=1, penable=0, ... }   // Setup 拍
        -> 进入 Access
    else:                                  // 译码失败
        弹出请求, 不发起 APB 访问
        返回 DECERR (或对空写返回 OKAY, 见 4.4)
state = Access:
    apb_req_o[sel_idx] = { psel=1, penable=1, ... }       // Access 拍
    if pready:
        弹出请求, 产生 B/R 响应 (pslverr -> resp)
        -> 回到 Setup
```

注意一个细节：只有 `psel` 选中的那一槽 `apb_req_o[sel_idx]` 会被赋值，其余槽保持默认 `'0`（即 `psel=0`）。

#### 4.3.3 源码精读

地址译码例化。`addr_decode` 的输出 `apb_sel_idx` 是从端索引，`apb_dec_valid` 指示地址是否命中规则表：

[src/axi_lite_to_apb.sv:267-280](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L267-L280)

```systemverilog
addr_decode #(
  .NoIndices( NoApbSlaves ), .NoRules( NoRules ),
  .addr_t( addr_t ), .rule_t( rule_t )
) i_apb_decode (
  .addr_i      ( apb_req.addr  ),
  .addr_map_i  ( addr_map_i    ),
  .idx_o       ( apb_sel_idx   ),
  .dec_valid_o ( apb_dec_valid ), // when not valid -> decode error
  ...
);
```

`Setup` 拍的处理。当请求合法、响应寄存器就绪、且译码成功、且（写事务至少有一个字节使能 / 读事务）时，给选中从端发出 Setup 相波形并转到 Access：

[src/axi_lite_to_apb.sv:295-314](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L295-L314)

```systemverilog
Setup: begin
  if (apb_req_valid && apb_wresp_ready && apb_rresp_ready) begin
    if (apb_dec_valid && ((apb_req.write && (|apb_req.strb)) || (!apb_req.write))) begin
      apb_req_o[apb_sel_idx] = '{
        paddr:   axi_pkg::aligned_addr(... apb_req.addr ...),  // 见 4.4
        pprot:   apb_req.prot,  psel: 1'b1,  penable: 1'b0,
        pwrite:  apb_req.write, pwdata: apb_req.data, pstrb: apb_req.strb
      };
      apb_state_d = Access;
      apb_update  = 1'b1;
    end else begin ... // 译码失败分支, 见 4.4
```

`Access` 拍的处理。`penable` 变 1，其余字段复用同样的赋值；当从端 `pready` 拉高时弹出请求、产生响应并回到 Setup：

[src/axi_lite_to_apb.sv:328-357](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L328-L357)

```systemverilog
Access: begin
  apb_req_o[apb_sel_idx] = '{ ... penable: 1'b1, ... };   // 与 Setup 同, 仅 penable 不同
  if (apb_resp_i[apb_sel_idx].pready) begin
    apb_req_ready = 1'b1;
    if (apb_req.write) begin
      apb_wresp       = apb_resp_i[apb_sel_idx].pslverr ? RESP_SLVERR : RESP_OKAY;
      apb_wresp_valid = 1'b1;
    end else begin
      apb_rresp.data  = apb_resp_i[apb_sel_idx].prdata;
      apb_rresp.resp  = apb_resp_i[apb_sel_idx].pslverr ? RESP_SLVERR : RESP_OKAY;
      apb_rresp_valid = 1'b1;
    end
    apb_state_d = Setup;
  end
end
```

状态寄存器用 `FFLARN` 宏（带 load enable 与异步复位、复位值为 `Setup`）：

[src/axi_lite_to_apb.sv:363](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L363) —— `apb_update` 作为 load enable，仅在状态变化时写寄存器，节省功耗。

#### 4.3.4 代码实践

**实践目标**：用测试台的断言理解「Setup 必须紧跟 Access」。

**操作步骤**：
1. 打开 [tb_axi_lite_to_apb.sv:190-196](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv#L190-L196)。
2. 阅读 `APB_TRANSFER` 序列（`APB_SETUP ##1 APB_ACCESS`）与 `apb_complete` 断言：`APB_SETUP |-> APB_TRANSFER`。

**需要观察的现象**：只要某拍出现 `psel=1 && penable=0`（Setup），下一拍必须是 `psel=1 && penable=1`（Access）。

**预期结果**：FSM 一旦从 `Setup` 转到 `Access`，`penable` 必然在下一拍拉高，断言永不被违反。如果你修改 FSM 让它在 Setup 后直接回 idle（不进 Access），这条断言会立刻报错——这就是本模块用断言把协议约束「钉死」的方式。待本地验证（运行 `make sim-tb_axi_lite_to_apb.log` 看是否 `Errors: 0`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Setup` 拍的条件里要检查 `apb_wresp_ready && apb_rresp_ready`？

> **答案**：FSM 在 Access 拍完成时会写响应寄存器（写响应 / 读响应各一个）。如果下游 AXI 侧还没准备好接收 B/R 响应（寄存器未就绪），FSM 就不能贸然进入会马上产生响应的访问流程，否则会丢响应。所以 Setup 拍先确认两个响应寄存器都能接住未来的响应，这是反压的一部分。

**练习 2**：地址译码失败时，`apb_req_o` 上会出现 `psel=1` 吗？

> **答案**：不会。译码失败走的是 `else` 分支，直接 `apb_req_ready=1` 弹出请求并返回错误响应，**完全不发起 APB 访问**，所有 `apb_req_o` 保持默认 `'0`，没有任何从端被选中。注释 [L22-L23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L22-L23) 明确这一点。

---

### 4.4 字段映射、地址对齐与错误码转换（含可选流水线与接口外壳）

#### 4.4.1 概念说明

把 Lite 翻译成 APB 时，大多数字段是 1:1 搬运（addr、prot、data、strb），但有三个关键转换点：

1. **地址对齐**：APB 规范 2.1.1 要求 `PADDR` 必须是总线宽度对齐的，否则行为不可预测。AXI4-Lite 的数据本身总是总线对齐的（即使地址未对齐），所以模块主动把地址按数据宽度对齐后再送给 APB。
2. **错误码翻译**：APB 只有一根 `PSLVERR`，而 AXI 有四档响应码。翻译规则是：`PSLVERR=1 → RESP_SLVERR`，`PSLVERR=0 → RESP_OKAY`；译码失败（地址不在映射表内）返回 `RESP_DECERR`。
3. **空写（写选通全 0）**：AXI 允许一笔「什么都不写」的事务（`wstrb=0`），这种事务不该真的去访问 APB 从端，模块对它直接返回 `RESP_OKAY`。

此外，模块还提供两个**可选流水线寄存器**开关与一个**接口外壳**，本节一并说明。

#### 4.4.2 核心流程

地址对齐用 `axi_pkg::aligned_addr`，定义见 [src/axi_pkg.sv:125-128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L125-L128)：

\[
\text{aligned\_addr}(addr, n) = \left\lfloor \frac{addr}{2^{n}} \right\rfloor \cdot 2^{n}
\]

即把地址的低 \(n\) 位清零。本模块调用时 \(n = \texttt{\$clog2(DataWidth/8)}\)：对于 32 位数据宽度，\(n = \texttt{\$clog2(4)} = 2\)，地址被对齐到 4 字节边界。

错误码与空写的翻译矩阵如下：

| 场景 | 译码结果 | write | strb | 返回响应 |
|------|---------|-------|------|---------|
| 正常读 | 命中 | 0 | — | `pready` 后按 `pslverr` 给 OKAY/SLVERR |
| 正常写 | 命中 | 1 | ≠0 | `pready` 后按 `pslverr` 给 OKAY/SLVERR |
| 空写（不写任何字节） | 命中或未命中 | 1 | =0 | **RESP_OKAY**（不发起 APB 访问） |
| 地址未命中映射 | 未命中 | 1 | ≠0 | **RESP_DECERR** |
| 地址未命中映射 | 未命中 | 0 | — | **RESP_DECERR** |

#### 4.4.3 源码精读

地址对齐在 Setup 与 Access 两次赋值 `paddr` 时都出现，调用同一表达式：

[src/axi_lite_to_apb.sv:302-312](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L302-L312)

```systemverilog
paddr: axi_pkg::aligned_addr(axi_pkg::largest_addr_t'(apb_req.addr), $clog2(DataWidth/8)),
```

先把地址宽展到 `largest_addr_t`（128 位，见 [src/axi_pkg.sv:120-123](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L120-L123)）再对齐，这是为了复用 `axi_pkg` 里那族要求 128 位入参的地址函数，综合器会把多余位优化掉。

译码失败分支的错误码翻译（注意空写的特殊处理）：

[src/axi_lite_to_apb.sv:316-325](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L316-L325)

```systemverilog
end else begin
  // decode error, generate error and do not generate APB request, pop it
  apb_req_ready = 1'b1;
  if (apb_req.write) begin
    apb_wresp       = ~(|apb_req.strb) ? axi_pkg::RESP_OKAY : axi_pkg::RESP_DECERR;
    apb_wresp_valid = 1'b1;
  end else begin
    apb_rresp.resp  = axi_pkg::RESP_DECERR;
    apb_rresp_valid = 1'b1;
  end
end
```

> 注意读响应的默认值是一个调试花 pattern：`apb_rresp = '{data: data_t'(32'hDEA110C8), resp: RESP_SLVERR}`（见 [L291](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L291)）。译码失败时只覆盖 `resp=DECERR`，`data` 仍保留 `0xDEA110C8`（读作 "DEA110C8"，类似 DEAD 的占位标记），方便在波形 / 日志里一眼认出「这是一笔未命中映射的读」。

**可选流水线寄存器**。请求通路和响应通路各有一个开关：`PipelineRequest` 控制仲裁器与 FSM 之间是否插寄存器，`PipelineResponse` 控制 B/R 响应是否插寄存器。两者都遵循同样的取舍：开 = `spill_register`（切断组合路径、增加 1 拍延迟，见讲义 u4-l1、u7-l1），关（默认）= `fall_through_register`（不增加延迟，仅在需要时锁存）：

[src/axi_lite_to_apb.sv:169-198](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L169-L198)（请求通路）、[src/axi_lite_to_apb.sv:200-256](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L200-L256)（响应通路）。

**接口外壳 `axi_lite_to_apb_intf`**。它把结构体端口换成 `AXI_LITE.Slave` 接口与扁平 APB 端口（`paddr_o/pselx_o/penable_o/...`），方便对接只认扁平信号的 APB IP。它内部用 `onehot_to_bin` 把 one-hot 的 `pselx_o` 转回二进制索引 `apb_sel`，再把选中槽的字段选出驱动扁平端口：

[src/axi_lite_to_apb.sv:454-472](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L454-L472)

```systemverilog
onehot_to_bin #( .ONEHOT_WIDTH(NoApbSlaves) ) i_onehot_to_bin (
  .onehot ( pselx_o ),  .bin ( apb_sel ) );
assign paddr_o   = apb_req[apb_sel].paddr;
// ... pprot_o/penable_o/pwrite_o/pwdata_o/pstrb_o 同理
for (genvar i = 0; i < NoApbSlaves; i++) begin : gen_apb_resp_assign
  assign pselx_o[i]          = apb_req[i].psel;
  assign apb_resp[i].pready  = pready_i[i];
  assign apb_resp[i].prdata  = prdata_i[i];
  assign apb_resp[i].pslverr = pslverr_i[i];
end
```

> 这里有个值得品味的细节：`pselx_o` 是模块**自己输出**的 one-hot 选片信号，外壳又把它 `onehot_to_bin` 转回二进制去索引 `apb_req` 数组——之所以这样「绕一圈」，是因为 `axi_lite_to_apb` 内核输出的就是每从端一份的 `apb_req_o` 数组，而扁平端口只需要被选中那一份的字段。这是「数组内核 ↔ 扁平端口」两种风格的桥接。

#### 4.4.4 代码实践

**实践目标**：动手验证「地址对齐」与「错误码翻译」两条规则。

**操作步骤**：
1. 在 `tb_axi_lite_to_apb` 的地址映射（[L74-L84](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv#L74-L84)）中，从端 0 覆盖 `0x0000_0000 ~ 0x0000_3000`。
2. 把随机 master 的 `MAX_ADDR` 暂时设为 `32'h0000_3000`（只命中合法区），跑一次仿真，观察读返回的 `r.data` 是否来自 APB `prdata`。
3. 再把 `MAX_ADDR` 扩到 `32'h0000_F000`（含未映射区），观察命中未映射地址时读响应的 `resp` 是否变成 `RESP_DECERR`、`data` 是否为 `0xDEA110C8`。

**需要观察的现象**：合法区读返回从端随机 `prdata`；未映射区读返回固定花 pattern 且响应为 DECERR。

**预期结果**：见错误码矩阵。待本地验证（无仿真器时，可改为静态走读：跟踪 [L298-L324](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L298-L324) 的分支条件得出同样结论）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 AXI4-Lite 数据「总线对齐」而地址可能未对齐？模块为什么仍要对齐地址？

> **答案**：AXI4-Lite 的 `W.data` 总是整总线宽度，但 `AW.addr` 可以是任意字节地址（配合 `wstrb` 选字节）。APB 规范 2.1.1 要求 `PADDR` 对齐到总线宽度，否则行为不可预测。所以模块用 `aligned_addr` 把地址的低 \(\texttt{\$clog2(DataWidth/8)}\) 位清零，保证送给 APB 的地址合法，字节选择信息则由 `pstrb` 承担。

**练习 2**：`PipelineRequest` 默认是 0（用 `fall_through_register`）。在什么情况下你会想把它改成 1？

> **答案**：当从 AXI-Lite 输入到 APB 输出之间的组合路径过长、成为关键路径、影响目标频率时，把 `PipelineRequest`（或 `PipelineResponse`）置 1，插入 `spill_register` 切断组合路径，用 1 拍延迟换时序裕量。这与讲义 u4-l1 中 `axi_cut` 的取舍完全一致。

---

### 4.5 测试台 tb_axi_lite_to_apb：随机从端与协议断言

#### 4.5.1 概念说明

`tb_axi_lite_to_apb` 是讲义 u3 确立的「定向随机验证」范式的典型实例：用 `axi_lite_rand_master` 发大量随机 Lite 事务，APB 从端用 `$urandom` 每拍随机更新 `pready/prdata/pslverr`（制造等待态与错误注入），再用 `assert property` 把 APB 协议时序钉死。它的价值不在「精确预言某个数据值」，而在「用随机激励 + 协议断言证明桥在任何合法 / 随机从端行为下都不违反 APB4 协议、不丢失响应」。

#### 4.5.2 核心流程

测试台结构：

```
axi_lite_rand_master  ──(AXI_LITE)──►  DUT(axi_lite_to_apb)  ──(APB)──►  8 个随机 APB 从端
        |                                                                        |
        |--- 跑 20000 读 + 10000 写 -------------------------------------------->|
                                                       ^
                                        assert property: 检查 APB 两相时序
```

时序三参数沿用讲义 u3-l3 的约定：`CyclTime=10ns`、`ApplTime=2ns`(TA)、`TestTime=8ns`(TT)，满足 \(0 < \text{TA} < \text{TT} < T_{\text{clk}}\)。

#### 4.5.3 源码精读

随机 master 的配置——注意 `MIN_ADDR/MAX_ADDR` 覆盖了从 `0x0` 到 `0x0002_2000` 的范围，故意**略大于**地址映射表（映射表最大到 `0x0002_1000`），从而确保会有一部分事务命中未映射区，触发 4.4 节的 DECERR 路径：

[tb_axi_lite_to_apb.sv:86-105](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv#L86-L105)。

随机 APB 从端——每个从端一个 `initial` 块，每个时钟沿用 `$urandom` 更新 `pready/prdata/pslverr`，模拟任意延迟与任意错误：

[tb_axi_lite_to_apb.sv:152-162](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv#L152-L162)

```systemverilog
for (genvar i = 0; i < NoApbSlaves; i++) begin : gen_apb_slave
  initial begin : proc_apb_slave
    apb_resps[i] <= '0;
    forever begin
      @(posedge clk);
      apb_resps[i].pready  <= #ApplTime $urandom();
      apb_resps[i].prdata  <= #ApplTime $urandom();
      apb_resps[i].pslverr <= #ApplTime $urandom();
    end
  end
end
```

APB 协议断言——用 4.1 节定义的序列检查：① `apb_complete`：Setup 必须紧跟 Access；② `apb_penable`：Access 且 `pready` 后 `penable` 必须撤；③ `control_stable`：传输期间 `pwrite/paddr` 必须稳定；④ `apb_valid`：传输期间控制信号不能含 `x`；⑤/⑥ `write_stable/strb_stable`：写事务 Access 期间 `pwdata/pstrb` 必须稳定：

[tb_axi_lite_to_apb.sv:195-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv#L195-L211)

```systemverilog
apb_complete:  assert property(@(posedge clk) (APB_SETUP |-> APB_TRANSFER));
apb_penable:   assert property(@(posedge clk)
                 (apb_req[i].penable && apb_req[i].psel && apb_resps[i].pready |=> (!apb_req[i].penable)));
control_stable:assert property(@(posedge clk) (APB_TRANSFER |-> $stable({apb_req[i].pwrite, apb_req[i].paddr})));
apb_valid:     assert property(@(posedge clk) (APB_TRANSFER |-> ((!{...}) !== 1'bx)));
write_stable:  assert property(@(posedge clk) ((apb_req[i].penable && apb_req[i].pwrite) |-> $stable(apb_req[i].pwdata)));
strb_stable:   assert property(@(posedge clk) ((apb_req[i].penable && apb_req[i].pwrite) |-> $stable(apb_req[i].pstrb)));
```

注意这些断言被 `ifndef VERILATOR` 包住（[L171](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_to_apb.sv#L171)），因为 Verilator 对 `assert property` 支持有限——这与讲义 u1-l4 讲到的「不同 EDA 工具兼容」策略一致。

#### 4.5.4 代码实践

**实践目标**：跑通这个测试台并解读日志。

**操作步骤**：
1. 在仓库根目录执行 `make sim-tb_axi_lite_to_apb.log`（讲义 u1-l4）。
2. 在生成的日志中找到 `Errors:` 统计行。

**需要观察的现象**：仿真跑完 30000 笔随机事务（20000 读 + 10000 写）后，`Errors: 0,` 且无 `assert property` 失败报告。

**预期结果**：日志出现 `Errors: 0,`，表示 master 全部事务都收到了响应、APB 协议断言全程未被违反。若工具链不可用，可改为静态走读：跟踪一笔写事务从 `aw_valid&w_valid` → 仲裁 → Setup → Access → `pready` → B 响应的完整链路，确认每一步都满足断言。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么测试台把随机 master 的地址上界设得比映射表还大（`0x0002_2000` > `0x0002_1000`）？

> **答案**：故意让一部分随机事务落到未映射区，从而在回归中覆盖「译码失败 → 返回 DECERR / 空写返回 OKAY」这条 4.4 节描述的分支，避免该分支成为未被测试的死角。

**练习 2**：随机从端用 `$urandom` 让 `pslverr` 随机翻转。这能验证模块的什么能力？

> **答案**：验证「`pslverr → resp` 翻译」在大量随机错误注入下仍正确——`pslverr=1` 必须翻译成 `RESP_SLVERR`，`pslverr=0` 翻译成 `RESP_OKAY`，且响应能正确路由到写（B）或读（R）通道。配合随机 master 的自检，就能闭环验证错误码路径。

---

## 5. 综合实践：自建一个简易 APB 从端并验证两相时序

**任务**：本库的测试台用「每拍全随机」的从端，它只用来制造压力、不校验功能。请你**替换**其中一个 APB 从端为一个「可预测」的简易模型，闭环验证一次 Lite 写能正确触发 APB 的 Setup/Access 两相并返回期望的 `pslverr`。

**步骤**：

1. **准备简易 APB 从端**。在 `tb_axi_lite_to_apb` 里，把 `gen_apb_slave` 中索引 0 的那个从端替换成一个简单的行为模型（示例代码，**非项目原有代码**）：

   ```systemverilog
   // 示例代码：一个确定性 APB 从端，占索引 0（地址 0x0~0x3000）
   // 它在 setup 拍锁存写数据，access 拍总是 ready，读返回固定 0xCAFE_BABE，
   // 并在访问地址为 0x2FFC 时回 pslverr=1 以模拟错误。
   logic [31:0] reg_file;
   initial begin
     apb_resps[0] <= '0;
     forever begin
       @(posedge clk);
       // 仅当本从端被选中
       if (apb_req[0].psel && apb_req[0].penable) begin
         apb_resps[0].pready  <= #ApplTime 1'b1;          // access 拍总 ready
         apb_resps[0].prdata  <= #ApplTime 32'hCAFE_BABE; // 读固定值
         apb_resps[0].pslverr <= #ApplTime (apb_req[0].paddr == 32'h2FFC);
         if (apb_req[0].pwrite) reg_file <= #ApplTime apb_req[0].pwdata;
       end else begin
         apb_resps[0] <= #ApplTime '0;
       end
     end
   end
   ```

2. **定向发一笔写**。把 `proc_axi_master` 里的 `run(...)` 临时改成定向写：用 `axi_lite_rand_master.write(32'h0000_0000, 32'h1234_5678, 4'b1111)`（`axi_lite_rand_master` 提供的定向 API，见讲义 u3-l2、u3-l3）。

3. **观察波形 / 日志**：确认 DUT 在 master 给出 AW+W 后：
   - 第一拍对从端 0 输出 `psel=1, penable=0`（Setup），地址已被对齐为 `0x0000_0000`；
   - 下一拍输出 `psel=1, penable=1`（Access），从端 `pready=1`；
   - master 的 B 通道收到 `resp=RESP_OKAY`。

4. **再发一笔读** `axi_lite_rand_master.read(32'h0000_2FFC, ...)`，确认：
   - APB 同样经历 Setup/Access 两相；
   - R 通道收到 `data=0xCAFE_BABE`、`resp=RESP_SLVERR`（因为 `0x2FFC` 触发了 `pslverr`）。

**预期结果**：写返回 OKAY 且 `reg_file` 被写成 `0x1234_5678`；读 `0x2FFC` 返回 `0xCAFE_BABE` 与 SLVERR；APB 协议断言全程不报错。这一闭环同时验证了 4.3 的两相时序、4.4 的地址对齐与错误码翻译。

> 若没有仿真器，可降级为「源码阅读型实践」：在 [src/axi_lite_to_apb.sv:282-361](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_to_apb.sv#L282-L361) 的 FSM 上手工推演上述两笔事务，逐一填出每个 `apb_req_o`、`apb_wresp`、`apb_rresp` 的取值与状态转移，得出相同结论。

## 6. 本讲小结

- `axi_lite_to_apb` 是全库**唯一的 APB 模块**（编译层级 Level 2），把 AXI4-Lite 单主端口桥接到最多 `NoApbSlaves` 个 APB4 从端。
- 它先把 Lite 的读、写请求归一成统一的 `int_req_t`，再用 `rr_arb_tree`（`LockIn=1`）串行化——因为 APB 是串行节拍协议，读、写不能并发。
- APB 输出由一个两态 FSM（`Setup` → `Access`）产生，`penable` 的 0/1 区分两拍；`addr_decode` 负责选片，译码失败不发起 APB 访问。
- 字段大多 1:1 搬运，但有三处关键转换：地址用 `axi_pkg::aligned_addr` 按 `DataWidth/8` 对齐；`pslverr` 翻译成 OKAY/SLVERR；译码失败返回 DECERR、空写（`wstrb=0`）返回 OKAY。
- `PipelineRequest` / `PipelineResponse` 两个开关分别给请求 / 响应通路选 `spill_register`（切路径、加延迟）或 `fall_through_register`（默认、零延迟）。
- 接口外壳 `axi_lite_to_apb_intf` 提供 `AXI_LITE.Slave` 接口与扁平 APB 端口，内部用 `onehot_to_bin` 在「数组内核」与「扁平端口」之间桥接。

## 7. 下一步学习建议

- **横向对比另一个协议桥**：阅读讲义 u13-l1 的 `axi_to_axi_lite` / `axi_lite_to_axi`，对比「双向握手协议（AXI/Lite）之间互转」与「单向节拍协议（APB）互转」在状态机复杂度上的差异——后者因没有 valid/ready 而需要 FSM 自己产生节拍。
- **学习地址译码的通用机制**：本模块的 `addr_decode` 与讲义 u6-l2 中 xbar 的地址译码同源，可结合 `axi_xbar_unmuxed` 与 `axi_err_slv` 系统理解「规则表 + 译码错误兜底」这一通用模式。
- **动手扩展**：若你的外设需要 APB3（无 `PREADY/PSLVERR`），可尝试基于本模块派生一个简化版，体会 `PREADY` 引入后 FSM 反压逻辑的必要性。
- **下一讲**：u14 进入「存储端点与适配器」，从 `axi_to_mem` 系列看 AXI 如何对接另一种非 AXI 存储协议（req/gnt/rvalid），与本讲的 APB 桥形成「AXI ↔ 异构协议」的完整拼图。
