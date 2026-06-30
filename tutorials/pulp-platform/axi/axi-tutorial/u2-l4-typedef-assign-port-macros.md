# typedef / assign / port 宏体系

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚本库「表达一个 AXI 端口」有哪 **三种等价方式**：`AXI_BUS` 接口、`req_t`/`resp_t` 结构体、扁平化端口，以及它们各自适合的场景。
- 用 `AXI_TYPEDEF_*` 系列宏，在一两行内声明一组通道结构体（AW/W/B/AR/R）和请求/响应结构体（`req_t`/`resp_t`）。
- 用 `AXI_ASSIGN_*` 系列宏，在 `AXI_BUS` 接口与 `req_t`/`resp_t` 结构体之间搬数据，并理解宏内部那个「分隔符戏法」是如何让一段代码同时服务于接口和结构体的。
- 理解 `AXI_M_PORT` / `AXI_S_PORT` 这类扁平端口宏存在的理由（Vivado IP Integrator 兼容），并能把它们和 `req_t`/`resp_t` 连起来。
- 仿照 `axi_xbar_intf` 的写法，为一个自定义模块套上「接口外壳 → 结构体内核」的标准结构。

## 2. 前置知识

本讲承接 **u2-l1 / u2-l2（axi_pkg 的类型与函数）** 与 **u2-l3（axi_intf 的 AXI_BUS 接口）**。在继续之前，请确保你心里有这两件事：

1. **AXI4 的五个通道**：写地址 AW、写数据 W、写响应 B、读地址 AR、读数据 R。每个通道都由「载荷（payload）」加「valid/ready 握手位」组成。这部分在 u1-l3 已经回顾过，u2-l1 又把它们落地成了 `axi_pkg::len_t`、`axi_pkg::burst_t`、`axi_pkg::resp_t`、`axi_pkg::atop_t` 等宽度固定的类型。

2. **SystemVerilog 宏基础**：本讲大量使用 `` `define `` 定义的文本宏。最关键的一个操作符是 **宏内连接（token paste）`` ` ``**。它把两段文本「粘」成一个标识符，例如：

   ```systemverilog
   `define NAME(prefix, suffix) prefix``_``suffix
   // 展开后：foo_bar
   ```

   这是本讲整套宏体系的「机械原理」。读不懂这个粘接操作，就看不懂 `assign.svh` 为什么能用一段代码同时改写接口信号和结构体字段。

### 为什么需要这套宏？——三种「表达 AXI 端口」的方式

写一个带 AXI 端口的模块时，你其实有三种风格可以选，它们**表达的是同一组信号**，但写法不同、用途不同：

| # | 风格 | 典型写法 | 信号命名 | 适合场景 |
|---|------|----------|----------|----------|
| ① | **接口（interface）** | `AXI_BUS.Slave slv` | 扁平：`slv.aw_id`、`slv.aw_valid` | 模块对外端口、可综合互联 |
| ② | **结构体（struct）** | `slv_req_t slv_req_i` | 嵌套：`slv_req.aw.id`、`slv_req.aw_valid` | 模块内部 datapath、参数化类型 |
| ③ | **扁平端口** | `input s_axi_my_awid, ...` | 全扁平：`m_axi_my_awid` | 顶层、Vivado IP Integrator |

> 注意 ① 和 ② 的微妙差别：在接口里，AW 通道的载荷是一组**以 `aw_` 为前缀的扁平信号**（`aw_id`、`aw_addr`、…）；而在结构体里，载荷被收拢进一个**嵌套子结构体 `aw`**，字段写成 `aw.id`、`aw.addr`。握手位 `aw_valid`/`aw_ready` 在两者里都挂在通道名后面。

三种风格各有优缺点：

- **接口**带 `modport`，能由编译器帮你检查主从方向，但接口类型很难做参数化（不同地址/ID 宽度要不同接口），也不被所有综合工具/IP 集成器接受。
- **结构体**是 `typedef` 出来的「普通类型」，可以作为 `parameter type` 传递、可以放进数组、可以参数化宽度，非常适合模块**内部**的 datapath；但它没有 modport 的方向保护。
- **扁平端口**是一根根裸线，最朴素、工具兼容性最好，但写起来冗长到难以维护。

一个真实的模块往往**同时**需要这几种风格：对外用接口 ① 方便连线，内核用结构体 ② 方便算术，顶层集成偶尔要扁平 ③。本库的三个宏文件就是用来在这三种风格之间**快速生成**和**无损搬运**的，避免你手写几十行样板代码、还容易写错信号名。

```
       typedef.svh                  assign.svh                   port.svh
   ┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
   │ 声明 ② 结构体    │◄────────│ ① ↔ ② ↔ ③ 搬运   │────────►│ 声明 ③ 扁平端口  │
   │ req_t / resp_t   │         │ AXI_ASSIGN_*     │         │ AXI_M/S_PORT    │
   └─────────────────┘         └──────────────────┘         └─────────────────┘
```

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [include/axi/typedef.svh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh) | 用宏生成 AXI / AXI-Lite 的通道结构体与 `req_t`/`resp_t` | 核心模块 ①：负责「声明结构体」 |
| [include/axi/assign.svh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh) | 在接口 / 结构体 / 扁平端口之间搬运信号 | 核心模块 ②：负责「在三种风格间连线」 |
| [include/axi/port.svh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/port.svh) | 生成扁平化的 AXI master/slave 端口声明 | 核心模块 ③：负责「生成扁平端口」 |
| [src/axi_intf.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv) | 定义 `AXI_BUS` 接口及其 modport | 对照参考：接口风格的信号命名 |
| [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv) | 交叉开关；其中的 `axi_xbar_intf` 把三者串起来 | 综合范例：最佳实践范本 |

> 这三个 `.svh` 是头文件，用 `` `include "axi/typedef.svh" `` 引入（路径前缀 `axi/` 由 Bender 的 `export_include_dirs` 提供，见 u1-l2）。它们本身不含任何可综合逻辑，只是一堆 `` `define ``。

---

## 4. 核心概念与源码讲解

### 4.1 typedef.svh：声明通道与 req/resp 结构体

#### 4.1.1 概念说明

回顾上一讲的 `axi_pkg`：它定义了**宽度固定的标量类型**，如 `len_t`、`burst_t`、`resp_t`。但一个 AXI 端口里，地址、数据、ID、user 的宽度是**每个模块自己定**的参数。所以你没法在 `axi_pkg` 里写死「一个 AW 通道长什么样」，而要在每个模块里，根据自己的 `addr_t`/`id_t`/`user_t` **现拼**一个 AW 结构体。

手写一个 AW 结构体有 12 个字段，W 有 4 个，B 有 3 个，AR 有 11 个，R 有 5 个，再加上 `req_t`/`resp_t` 把它们和握手位打包……一个端口就要写大几十行，而且极易漏字段。`typedef.svh` 把这件事压缩成了几行宏调用。

#### 4.1.2 核心流程

使用流程是「先定基础位宽类型，再调宏声明通道，最后打包成 req/resp」：

1. 用 `typedef logic [...] addr_t;` 等声明本模块用的基础类型。
2. 对每个通道调一次 `AXI_TYPEDEF_<CHAN>_CHAN_T(新类型名, 所需子类型…)`，宏会展开成一条 `typedef struct packed { ... } 新类型名;`。
3. 调 `AXI_TYPEDEF_REQ_T` / `AXI_TYPEDEF_RESP_T` 把通道结构体和握手位打包成 `req_t` / `resp_t`。
4. 若嫌逐个声明太啰嗦，可直接用 `AXI_TYPEDEF_ALL_CT` / `AXI_TYPEDEF_ALL` 一次声明全部。

#### 4.1.3 源码精读

**(1) `AXI_DECL_*` 与 `AXI_TYPEDEF_*` 的区别**——前者只给 `struct packed {...}` 本体，后者再套一层 `typedef`：

[include/axi/typedef.svh:35-51](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L35-L51) 定义 AW 通道。注意它把 AXI5 的 `atop`（原子操作编码）也包含在内，这正是库内类型叫「AXI4+ATOPs」的体现；而读地址 AR 通道没有 `atop`：

[include/axi/typedef.svh:69-84](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L69-L84) 是 AR 通道，字段比 AW 少一个 `atop`。这种「AW 带 atop、AR 不带」的不对称，是后续讲义讲原子操作（u15-l1）的基础。

**(2) `req_t` / `resp_t` 的打包**——这是整个库端口设计的核心数据结构：

[include/axi/typedef.svh:95-119](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L95-L119) 把三个**请求方向**通道（aw、w、ar）及其 `valid`、加上两个响应方向的 `ready`（b_ready、r_ready），打包进 `req_t`；把三个**响应方向**的 `ready`（aw/ar/w_ready）、两个响应通道（b、r）及其 `valid`，打包进 `resp_t`。请记住这个划分原则——它直接对应 AXI 的主从方向：

- **`req_t`（请求方发出的信号）**：AW/W/AR 的载荷 + AW/W/AR 的 `valid` + B/R 的 `ready`。
- **`resp_t`（响应方发出的信号）**：AW/W/AR 的 `ready` + B/R 的载荷 + B/R 的 `valid`。

一个从端口因此就是 `input req_t` + `output resp_t` 两个端口，干净地把「我向对方要什么」和「对方给我什么」分开。

**(3) 一次性声明全部**——`AXI_TYPEDEF_ALL_CT`：

[include/axi/typedef.svh:134-141](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L134-L141) 一次调用就声明五个通道结构体（命名形如 `axi_aw_chan_t`）外加 `req_t`/`resp_t`。`__name``_aw_chan_t` 里的 `` ` `` 就是第 2 节讲的宏内连接：把传入的 `__name` 和后缀 `_aw_chan_t` 粘成一个类型名。`AXI_TYPEDEF_ALL`（[L156-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L156-L157)）是它的简写，连 req/resp 的名字都用约定俗成的 `__name``_req_t`。

> 此外文件后半段（[L172-271](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L172-L271)）是 AXI-Lite 版本 `AXI_LITE_TYPEDEF_*`，字段更少（无 len/size/burst/id/atop），命名与套路与完整版完全对称，本讲不再逐行展开。

#### 4.1.4 代码实践

**实践目标**：亲手把一个宏调用「手动展开」一次，建立「宏 = 文本替换」的直觉。

**操作步骤**：

1. 打开 [include/axi/typedef.svh:59-60](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L59-L60)，找到 `AXI_TYPEDEF_W_CHAN_T` 的定义，它展开后等价于：
   ```systemverilog
   // 调用：`AXI_TYPEDEF_W_CHAN_T(my_w_t, my_data_t, my_strb_t, my_user_t)
   typedef struct packed {
     my_data_t data;
     my_strb_t strb;
     logic     last;
     my_user_t user;
   } my_w_t;
   ```
2. 在纸上（或一个临时 `.sv` 文件里）写出下列两行调用的展开结果：
   ```systemverilog
   `AXI_TYPEDEF_B_CHAN_T(my_b_t, my_id_t, my_user_t)
   `AXI_TYPEDEF_RESP_T(my_resp_t, my_b_t, my_r_t)
   ```

**需要观察的现象**：展开后 `my_resp_t` 里应该出现 `aw_ready`、`ar_ready`、`w_ready`、`b_valid`、`b`、`r_valid`、`r` 七个字段——正好对应第 (2) 点里描述的 resp_t 结构。

**预期结果**：你的展开结果与 [include/axi/typedef.svh:108-119](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L108-L119) 里的 `AXI_DECL_RESP_T` 一模一样（字段顺序也一致）。若不一致，多半是漏了某个握手位。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AXI_TYPEDEF_AW_CHAN_T` 的参数里有 `atop` 相关内容，而 `AXI_TYPEDEF_AR_CHAN_T` 没有？

> **答案**：AXI 的原子操作（ATOP）编码只挂在**写地址** AW 通道上（见 u2-l1 对 `atop_t` 的讲解），读地址 AR 不需要。所以 AW 结构体多一个 `axi_pkg::atop_t atop` 字段（[L47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/typedef.svh#L47)），AR 没有。

**练习 2**：`req_t` 里为什么会出现 `b_ready` 和 `r_ready`，而不是 `b_valid` / `r_valid`？

> **答案**：`req_t` 收集的是「请求方这一侧驱动的信号」。B/R 是响应通道，对请求方来说，它在这两个通道上驱动的是 `ready`（表示自己准备好接收响应），而 `valid` 与载荷是由响应方驱动的，因此归入 `resp_t`。

---

### 4.2 assign.svh：在接口与结构体之间搬数据

#### 4.2.1 概念说明

有了 `req_t`/`resp_t`，模块内核可以很方便地用 `req.aw.addr` 这样的嵌套字段做算术。但模块对外端口往往用的是 `AXI_BUS` 接口（风格 ①），它的信号是扁平的 `aw_addr`。于是内核算出的结构体值，要「灌」进接口；接口收到的信号，要「读」进结构体。

这件事本质上是几十条 `assign slv.aw_addr = req.aw.addr;` 这样的逐字段赋值。`assign.svh` 把这些逐字段赋值封装成宏，并玩了一个聪明的把戏：**用同一份「字段清单」同时描述接口和结构体两种命名**。

#### 4.2.2 核心流程

`assign.svh` 的宏按「方向 × 位置」分成几组，记忆口诀是 **`FROM` / `TO` × `REQ` / `RESP`**：

- `AXI_ASSIGN_FROM_REQ(if, req)`：把 `req` 结构体的值**写进**接口 `if`（接口作目的端）。
- `AXI_ASSIGN_TO_REQ(req, if)`：把接口 `if` 的值**读进** `req` 结构体（结构体作目的端）。
- `AXI_ASSIGN_FROM_RESP` / `AXI_ASSIGN_TO_RESP` 同理，针对响应方向。
- 名字里没有 `FROM`/`TO` 的 `AXI_ASSIGN(slv, mst)` 是接口↔接口的整口直连。
- 带 `SET` 的（`AXI_SET_FROM_*`）用于 `always` 进程**内部**（不带 `assign` 关键字）；带 `ASSIGN` 的用于进程**外部**（带 `assign`）。

#### 4.2.3 源码精读

**(1) 分隔符戏法——整份文件的灵魂**：

[include/axi/assign.svh:26-38](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L26-L38) 是内部宏 `__AXI_TO_AW`，它为 AW 通道的每个字段生成一条赋值，形如：

```systemverilog
__opt_as __lhs``__lhs_sep``id = __rhs``__rhs_sep``id;
```

关键在两个分隔符参数 `__lhs_sep` 和 `__rhs_sep`：

- 接口侧用 `_`：`__lhs=axi_if.aw`、`__lhs_sep=_` → 粘出 `axi_if.aw_id`（接口的扁平信号）。
- 结构体侧用 `.`：`__rhs=req.aw`、`__rhs_sep=.` → 粘出 `req.aw.id`（结构体的嵌套字段）。

于是**同一份字段清单**，只要换一个分隔符，就能既赋值接口、又赋值结构体。这就是为什么 `__AXI_TO_REQ`（[L66-74](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L66-L74)）会先调 `__AXI_TO_AW(..., __lhs.aw, __lhs_sep, ...)`——那个 `.aw` 是写死在宏里的文本片段，配合 `_` 变成 `aw_id`、配合 `.` 变成 `aw.id`。

> 小提示：这里的 `__opt_as` 也很巧妙。它取值为 `assign` 时，生成的是进程外的连续赋值语句；取值为空（宏里写 `, ` 留空）时，生成的是可放进 `always` 块的过程赋值。一套内部宏，两套用法。

**(2) 用户层宏 `AXI_ASSIGN_FROM_REQ` / `AXI_ASSIGN_TO_RESP`**：

[include/axi/assign.svh:200-201](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L200-L201) 把接口放在左侧、结构体放在右侧，分隔符分别填 `_` 和 `.`：

```systemverilog
`define AXI_ASSIGN_FROM_REQ(axi_if, req_struct)  `__AXI_TO_REQ(assign, axi_if, _, req_struct, .)
```

展开后等价于：

```systemverilog
assign axi_if.aw_id   = req_struct.aw.id;
assign axi_if.aw_addr = req_struct.aw.addr;
// ... 其余 AW 字段 ...
assign axi_if.aw_valid = req_struct.aw_valid;
// ... W / AR 通道与 b_ready / r_ready ...
```

注意握手位 `aw_valid` 是结构体 `req_t` 的**顶层字段**（不是 `aw` 的子字段，见 4.1.3 的 req_t 结构），所以 `__AXI_TO_REQ` 里对它的赋值是单独写的（[L68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L68)），不走分隔符戏法。

**(3) 接口↔接口的整口直连**：

[include/axi/assign.svh:119-124](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L119-L124) 的 `AXI_ASSIGN(slv, mst)` 把 `mst` 当主、`slv` 当从整口连起来。注意它对 B/R 通道调的是 `AXI_ASSIGN_B(mst, slv)`（参数顺序反过来），因为响应方向是 slave→master。这正是 u2-l3 讲的「valid 由发出方驱动、ready 由接收方驱动」在宏里的体现。

#### 4.2.4 代码实践

**实践目标**：在一个最小模块里，用 `AXI_ASSIGN_TO_REQ` / `AXI_ASSIGN_FROM_RESP` 把 `AXI_BUS.Slave` 接口转成 `req_t`/`resp_t`，体验「接口外壳 + 结构体内核」的标准骨架。

**操作步骤**（这是「示例代码」，不在仓库中实际存在，仅供你照着写）：

```systemverilog
`include "axi/assign.svh"
`include "axi/typedef.svh"

module my_passthrough #(
  parameter int unsigned AW = 32, DW = 64, IW = 4, UW = 0
) (
  input  logic clk, input logic rst_n,
  AXI_BUS.Slave  in,
  AXI_BUS.Master out
);
  // 1) 基础位宽类型
  typedef logic [AW-1:0] addr_t;
  typedef logic [DW-1:0] data_t;
  typedef logic [DW/8-1:0] strb_t;
  typedef logic [IW-1:0] id_t;
  typedef logic [UW-1:0] user_t;
  // 2) 用宏声明通道与 req/resp
  `AXI_TYPEDEF_ALL_CT(my, my_req_t, my_resp_t, addr_t, id_t, data_t, strb_t, user_t)
  // 3) 内部结构体变量
  my_req_t  in_req,  out_req;
  my_resp_t in_resp, out_resp;
  // 4) 接口 ↔ 结构体
  `AXI_ASSIGN_TO_REQ   (in_req,  in)      // 接口的值读进 in_req
  `AXI_ASSIGN_FROM_RESP(in,      in_resp) // in_resp 写回接口
  `AXI_ASSIGN_FROM_REQ (out,     out_req)
  `AXI_ASSIGN_TO_RESP  (out_resp, out)
  // 5) 内核：直通（后续讲义的模块会在这里做真正的逻辑）
  assign out_req  = in_req;
  assign in_resp  = out_resp;
endmodule
```

**需要观察的现象**：注意第 4 步里 `in` 作为 slave 接口，请求方向用 `TO_REQ`（读入）、响应方向用 `FROM_RESP`（写出）；`out` 作为 master 接口则相反。这套「slave 配 TO_REQ/FROM_RESP，master 配 FROM_REQ/TO_RESP」的搭配，就是 `axi_xbar_intf` 里反复出现的模式。

**预期结果**：在支持 SystemVerilog 的仿真器里 elaborate 该模块应无报错；功能上 `out` 口完全跟随 `in` 口。若你暂时没有仿真环境，本步骤可作为「源码阅读型实践」——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`AXI_ASSIGN_FROM_REQ(if, req)` 和 `AXI_SET_FROM_REQ(if, req)` 有什么区别？分别该用在哪里？

> **答案**：前者展开时 `__opt_as = assign`，生成的是模块体内、进程外的连续赋值语句；后者 `__opt_as` 为空，生成的是不带 `assign` 的过程赋值，必须放在 `always_comb` / `always_ff` 等**进程内部**。需要和其他逻辑组合赋值同一接口时，用 `SET` 版本放进 `always` 块；纯连线时用 `ASSIGN` 版本。

**练习 2**：为什么 `AXI_ASSIGN(slv, mst)` 内部对 B 通道调用的是 `AXI_ASSIGN_B(mst, slv)` 而不是 `AXI_ASSIGN_B(slv, mst)`？

> **答案**：`AXI_ASSIGN_B(dst, src)` 的语义是「载荷和 `valid` 从 src 流向 dst，`ready` 从 dst 流回 src」。B 通道的载荷与 `b_valid` 是 slave 发给 master 的，所以要让 master 当 `dst`、slave 当 `src`，方向才对。

---

### 4.3 port.svh：扁平化端口与 Vivado 兼容

#### 4.3.1 概念说明

绝大多数模块用接口 ① 或结构体 ② 就够了。但有一种场景必须用**扁平端口**③：Xilinx Vivado IP Integrator（以及部分只认扁平端口的工具流）。这类工具要求每个 AXI 信号都是一根独立的顶层端口，且遵循固定的命名约定（如 `m_axi_<bus>_awvalid`）。

手写一整套扁平端口（AW 12 根 + W 5 根 + B 4 根 + AR 12 根 + R 6 根 + 5 个 ready，共约 44 根）既枯燥又易错。`port.svh` 用两个宏 `AXI_M_PORT` / `AXI_S_PORT` 把这件事一次搞定。

#### 4.3.2 核心流程

1. 在模块端口列表里调用 `AXI_M_PORT(name, addr_t, data_t, strb_t, id_t, aw_user_t, w_user_t, b_user_t, ar_user_t, r_user_t)`，它展开成一组以 `m_axi_name_` 为前缀的端口声明。
2. slave 侧用 `AXI_S_PORT`，前缀变成 `s_axi_name_`，方向（input/output）与 master 整体对调。
3. 在模块体内，用 `assign.svh` 提供的 `AXI_ASSIGN_MASTER_TO_FLAT` / `AXI_ASSIGN_SLAVE_TO_FLAT` 把这些扁平端口和 `req_t`/`resp_t` 连起来（这两个搬运宏其实定义在 `assign.svh` 里，见 4.3.3）。

#### 4.3.3 源码精读

**(1) master 扁平端口声明**：

[port.svh:23-67](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/port.svh#L23-L67) 的 `AXI_M_PORT` 展开后是一长串 `output ... m_axi_``__name``_awvalid`、`input ... m_axi_``__name``_bvalid` 这样的端口。命名严格遵循 Vivado 约定（`m_axi_` 前缀 + 总线名 + 信号名）。注意每个通道的 user 可以单独指定宽度，所以参数列表里有五个 `__x_user_t`。

**(2) slave 扁平端口声明**：

[port.svh:73-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/port.svh#L73-L117) 的 `AXI_S_PORT` 与 master 镜像，前缀 `s_axi_`，`input`/`output` 方向整体互换。

**(3) 扁平端口 ↔ 结构体的搬运**（注意：这组宏住在 `assign.svh`，不在 `port.svh`）：

[include/axi/assign.svh:547-598](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L547-L598) 的 `AXI_ASSIGN_MASTER_TO_FLAT(pat, req, rsp)` 把 `req`/`rsp` 结构体逐字段连到 `m_axi_``pat`_... 扁平端口。`AXI_ASSIGN_SLAVE_TO_FLAT`（[L600-651](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L600-L651)）是 slave 版本。此外还有把扁平端口直接做成实例化连接端口列表的 `AXI_ASSIGN_TO_FLAT_PORT` / `AXI_ASSIGN_MASTER_TO_FLAT_PORT` / `AXI_ASSIGN_SLAVE_TO_FLAT_PORT`（[L653-710](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L653-L710)），用于 `.port(signal)` 风格的例化。

> 风格 ③（扁平端口）在本库的可综合 RTL 里其实很少出现——`port.svh` 主要服务「需要把本库模块包成 Vivado IP」的外部用户。本库自身的模块（包括 `axi_xbar`）几乎都用结构体 ②，必要时套一层 `*_intf` 接口外壳提供 ①。

#### 4.3.4 代码实践

**实践目标**：阅读 `port.svh`，理解扁平端口与结构体的字段一一对应关系。

**操作步骤**：

1. 打开 [port.svh:23-67](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/port.svh#L23-L67)，数一下 `AXI_M_PORT` 展开后 master 侧一共有多少个端口，并按 AW/W/B/AR/R 五个通道分组计数。
2. 对照 [assign.svh:547-598](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/include/axi/assign.svh#L547-L598) 的 `AXI_ASSIGN_MASTER_TO_FLAT`，确认这组搬运宏对每个扁平端口都恰好有一条 `assign`，且左右两边的字段名（如 `req.aw.id` ↔ `m_axi_pat_awid`）一一对应。

**需要观察的现象**：扁平端口的数量应当与 4.1 里 req_t+resp_t 的字段总数一致（约 44 个），多一个或少一个都说明某根信号漏了。

**预期结果**：计数应为 AW 通道 13 个（含 awvalid/awready）+ W 通道 6 + B 通道 4 + AR 通道 13 + R 通道 7 ≈ 43 根，与 u2-l3 里 `AXI_BUS` 接口的信号数吻合——再次印证三种风格表达的是同一组信号。精确逐项核对**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`AXI_M_PORT` 和 `AXI_S_PORT` 的区别，除了前缀（`m_axi_` vs `s_axi_`）还有什么？

> **答案**：每一根信号的方向（`input`/`output`）整体互换。例如 `awvalid` 在 master 端口里是 `output`，在 slave 端口里是 `input`；`bvalid` 则相反。这与 modport 区分 Master/Slave 的逻辑一致（见 u2-l3）。

**练习 2**：既然本库自身几乎不用扁平端口，为什么 `port.svh` 还要维护？

> **答案**：为了让外部用户能把本库模块包装成符合 Vivado IP Integrator 命名约定的顶层 IP。扁平端口是这类 EDA 工具流的硬性要求，不是设计偏好问题。

---

## 5. 综合实践

**任务**：仿照 `axi_xbar_intf` 的写法，给一个「空壳」模块套上完整的「接口外壳 → 结构体内核」结构，把本讲三个最小模块串起来用一遍。

**范本精读**：先读懂 [src/axi_xbar.sv:159-219](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L159-L219) 这个 `axi_xbar_intf` 模块，它是本讲的最佳实践范本，结构分四步：

1. **include 两个头文件**（[L159-160](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L159-L160)）：先 `assign.svh` 再 `typedef.svh`（顺序无所谓，但两个都要 include，宏才可用）。
2. **声明基础类型 + 调宏声明通道/req/resp**（[L185-204](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L185-L204)）：注意 master 侧和 slave 侧的 ID 宽度不同（master 更宽，见 u6-l1），所以分别声明了两套 `aw_chan_t`/`b_chan_t`/…。
3. **声明结构体数组**（[L206-209](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L206-L209)）：`mst_req_t [NoMstPorts-1:0] mst_reqs;` 等，每个端口一个结构体。
4. **在 `for` 生成块里用搬运宏把接口和结构体连起来**（[L211-219](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L211-L219)）：master 端口配 `AXI_ASSIGN_FROM_REQ`/`AXI_ASSIGN_TO_RESP`，slave 端口配 `AXI_ASSIGN_TO_REQ`/`AXI_ASSIGN_FROM_RESP`。连好之后，真正的内核 `axi_xbar`（用结构体端口，[L221-249](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L221-L249)）只面对 `req_t`/`resp_t`，完全不用管接口的扁平信号。

**你要做的事**：参照 4.2.4 的 `my_passthrough` 骨架，扩展成一个有 **1 个 `AXI_BUS.Slave` 输入接口和 1 个 `AXI_BUS.Master` 输出接口** 的模块，要求：

- 用 `AXI_TYPEDEF_ALL_CT` 一次性声明 `req_t`/`resp_t`；
- 用 `AXI_ASSIGN_TO_REQ` / `AXI_ASSIGN_FROM_RESP` 处理 slave 接口，用 `AXI_ASSIGN_FROM_REQ` / `AXI_ASSIGN_TO_RESP` 处理 master 接口；
- 在内核里写一句 `assign out_req = in_req; assign in_resp = out_resp;` 实现直通；
- 解释：为什么 slave 接口请求方向用 `TO_REQ` 而 master 接口请求方向用 `FROM_REQ`？

**验收**：在仿真器里 elaborate 通过、功能上输出跟随输入即可（综合非必需）。若本地无仿真器，至少完成「源码阅读 + 手写展开」，并把对最后那个问题的回答写进你的学习笔记。运行结果**待本地验证**。

**参考答案（方向问题）**：`TO_REQ(req, if)` 是「把接口的值读进 req」，slave 接口上的请求信号是**由外部 master 驱动进来**的，所以要读进模块内的 req 结构体；`FROM_REQ(if, req)` 是「把 req 的值写进接口」，master 接口上的请求信号是**由本模块驱动出去**的，所以要从内部 req 写出去。响应方向同理，方向相反。

## 6. 本讲小结

- 本库用**三种等价风格**表达同一个 AXI 端口：`AXI_BUS` 接口（扁平 `aw_id`）、`req_t`/`resp_t` 结构体（嵌套 `aw.id`）、扁平端口（`m_axi_x_awid`）。三者表达的是同一组约 43 根信号。
- **typedef.svh** 用 `AXI_TYPEDEF_*` 宏声明通道结构体和 `req_t`/`resp_t`；`req_t` 装「请求方驱动」的信号（AW/W/AR 载荷+valid、B/R ready），`resp_t` 装「响应方驱动」的信号。
- **assign.svh** 靠一个「分隔符戏法」（`_` 给接口、`.` 给结构体）让同一份字段清单同时服务两种命名；用户宏按 `FROM/TO` × `REQ/RESP` × `SET/ASSIGN` 组合命名。
- **port.svh** 用 `AXI_M_PORT`/`AXI_S_PORT` 生成符合 Vivado 命名约定的扁平端口，主要服务 IP 集成场景；它与 `req_t`/`resp_t` 的连接由 `assign.svh` 里的 `AXI_ASSIGN_*_TO_FLAT` 系列完成。
- `axi_xbar_intf` 是把三者串起来的范本：include 头文件 → `AXI_TYPEDEF_ALL_CT` 声明结构体 → 结构体数组 → `for` 块里用搬运宏把接口和结构体一一连接，内核只面对 `req_t`/`resp_t`。

## 7. 下一步学习建议

- 下一讲 **u3-l1（axi_test 底层驱动）** 会用到本讲的 `AXI_BUS_DV` 接口与 `req_t`/`resp_t` 结构体——driver 类正是驱动这些结构体字段来逐拍产生激励的，本讲是它的直接前置。
- 在进入 u3 之前，建议随手翻一个真实 testbench（如 `test/tb_axi_lite_regs.sv`），找其中的 `AXI_LITE_TYPEDEF_*` 与 `AXI_LITE_ASSIGN_*` 调用，对照本讲理解 Lite 版宏的简化（无 id/len/burst）。
- 若想看一个把「接口外壳 + 结构体内核」模式用到极致的例子，直接精读 [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv) 的 `axi_xbar_intf`（L162 起），它是 u6 单元（交叉开关）的入口，本讲已为你打好读它的语法基础。
