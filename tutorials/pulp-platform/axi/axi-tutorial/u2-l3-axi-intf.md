# axi_intf：SystemVerilog 接口

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 SystemVerilog 的 `interface` 与 `modport` 解决了什么问题，为什么本库要用它而不是裸端口列表。
- 看懂 `axi_intf.sv` 里定义的 `AXI_BUS` 接口：它如何把 AXI4 的五个通道（AW/W/B/AR/R）几十根信号收进一个对象，并用 `Master` / `Slave` / `Monitor` 三个 modport 区分主、从、监听三种视角。
- 理解 `AXI_BUS_DV` 在 `AXI_BUS` 基础上多出一个 `clk_i` 时钟端口，并据此挂上一组协议断言（assertion），从而成为验证专用接口。
- 区分完整 AXI4 接口与精简的 `AXI_LITE` / `AXI_LITE_DV`，了解异步（CDC）变体的存在。
- 能照着本库的真实写法，写一个挂在 `AXI_BUS_DV.Monitor` 上、用 `always_ff` 采样 AW 握手并把 `id`/`addr` 打印出来的最小 monitor 模块。

本讲是「基础设施三件套」的第二件——承接 u2-l1 / u2-l2 对 `axi_pkg` 类型与常量的讲解，把那些 `axi_pkg::len_t`、`axi_pkg::burst_t` 等类型真正铺到「线」上；并为 u2-l4 的 `typedef` / `assign` / `port` 宏体系铺路。

## 2. 前置知识

在进入源码前，先用大白话建立三个概念。

**接口（interface）**：模块的端口列表里如果每个信号都单列一行，一个 AXI4 主端口就要写三十多个信号，两个模块对接时还要逐根连、逐根对方向，既啰嗦又容易错。SystemVerilog 的 `interface` 把一组相关信号打包成一个「bundle」，连接时整根对象一接即可，等于把一捆线绑成一根粗缆。

**modport**：光打包还不够——同一个 bundle，主设备对 `aw_valid` 是「输出」、从设备对 `aw_valid` 是「输入」。`modport` 就是在同一个接口里预先定义好几套「视角」，声明 `AXI_BUS.Master` 就是「我是主，这些信号我输出、那些我输入」，声明 `AXI_BUS.Slave` 则方向相反。编译器据此做方向检查，连反了会报错。

**AXI4 的五通道与握手**：复习 u1-l3——写事务走 AW（写地址）/ W（写数据）/ B（写响应），读事务走 AR（读地址）/ R（读数据）；每根通道都是一对 `valid`/`ready` 握手信号，同一时钟沿两者同高才算一次成功的「拍」（beat）。本讲就是把这五通道 + 握手信号装进 `interface`。

> 一个在本库反复出现的术语区分：`in flight`（在途）指一个事务的地址拍已握手、但响应拍还没握手；`pending`（挂起）指某根通道上 `valid` 已拉高但 `ready` 还没拉高（拍级）。本讲的断言部分主要守护的是「`pending` 期间信号不能变」这一拍级规则。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [src/axi_intf.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv) | 定义本库全部 AXI 接口：`AXI_BUS`、`AXI_BUS_DV`、`AXI_BUS_ASYNC`、`AXI_BUS_ASYNC_GRAY`、`AXI_LITE`、`AXI_LITE_DV`、`AXI_LITE_ASYNC_GRAY` |

为了让讲解落到「真实用法」上，本讲还会引用两个使用这些接口的样本：

| 文件 | 作用 |
| --- | --- |
| [src/axi_join.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv) | `axi_join_intf`：一个用 `AXI_BUS.Slave` + `AXI_BUS.Master` 把两个接口直连的最小模块 |
| [src/axi_dumper.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv) | `axi_dumper_intf`：全库唯一真正使用 `AXI_BUS_DV.Monitor` modport 的模块，是本讲实践的范本 |
| [test/tb_axi_modify_address.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_modify_address.sv) | 真实测试台，展示如何声明 `AXI_BUS_DV`、把 `clk` 接进 `.clk_i`、再用 `AXI_ASSIGN` 在 DV 接口与 `AXI_BUS` 之间搬数据 |

## 4. 核心概念与源码讲解

### 4.1 interface 与 modport：为什么要打包信号

#### 4.1.1 概念说明

如果不用 `interface`，一个 AXI4 主端口的端口列表会长成这样（节选）：

```systemverilog
// 反面教材：裸端口列表，仅 AW 通道就已经这么长
output logic [ID-1:0]   aw_id,
output logic [ADDR-1:0] aw_addr,
output logic [7:0]      aw_len,
output logic [2:0]      aw_size,
output logic [1:0]      aw_burst,
output logic            aw_lock,
output logic            aw_valid,
input  logic            aw_ready,
// ... W / B / AR / R 通道还有几十行 ...
```

问题有三个：一是冗长；二是主从方向要人脑记忆并手写对调，连反不报错只是仿真出诡异行为；三是顶层例化时要逐根 `.aw_id(aw_id)` 连线，纯体力活。SystemVerilog 的 `interface` 把整组信号收成一个对象，`modport` 再为「主、从、监听」预设好方向，三害一起解。

#### 4.1.2 核心流程

一个接口在工程里的生命周期：

1. 在某个文件里用 `interface ... endinterface` 定义一个 bundle，内部声明所有信号，并用若干 `modport` 列出不同视角的方向。
2. 模块的端口列表里直接用 `接口名.视角` 声明端口，例如 `AXI_BUS.Slave in`，意思是「我这边是从视角」。
3. 顶层把两个模块「一接」：声明一个 `AXI_BUS #(...) bus ();` 实例，分别连到主模块的 `AXI_BUS.Master` 端口和从模块的 `AXI_BUS.Slave` 端口，整捆信号一次性对接。
4. 在接口内部，还可以放 `localparam`、`typedef`，甚至用 `clk_i` 触发的 `assert property`，把「和这捆信号强相关」的常量与检查就近放在一起。

#### 4.1.3 源码精读

`AXI_BUS` 接口的开头展示了「参数 + 派生 localparam + 派生 typedef」的标准三段式：先用四个宽度参数定义接口的位宽，再据此算出字节使能宽度 `AXI_STRB_WIDTH`，最后为每种位宽起一个简短的类型别名（`id_t`、`addr_t` 等），供接口内信号声明直接使用：

[src/axi_intf.sv:20-33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L20-L33) — 接口参数、派生的 `AXI_STRB_WIDTH` 与五个 typedef。其中字节使能宽度恒满足

\[
\text{AXI\_STRB\_WIDTH} = \text{AXI\_DATA\_WIDTH} / 8
\]

即每 8 位数据配 1 比特 `wstrb`。

注意接口内大量信号直接引用了 `axi_pkg` 的类型（如 `axi_pkg::len_t aw_len;`），这正是 u2-l1 讲过的「`axi_pkg` 是全库类型单一事实来源」在接口里的体现——接口不自己重新定义 `len_t`，而是去 `import`/`axi_pkg::` 引用它。

#### 4.1.4 代码实践

**实践目标**：感受「裸端口 vs 接口」在书写量上的差异。

**操作步骤**：

1. 打开 [src/axi_intf.sv:35-83](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L35-L83)，数一下 `AXI_BUS` 内部一共声明了多少根信号（提示：AW 14 根、W 5 根、B 5 根、AR 13 根、R 6 根，约 43 根）。
2. 想象用裸端口列表写一个 `AXI_BUS.Master`：这 43 根里，哪些该是 `output`、哪些该是 `input`？
3. 对比 [src/axi_join.sv:19-24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv#L19-L24)——这个把两个接口直连的模块，端口只需两行 `AXI_BUS.Slave in` / `AXI_BUS.Master out`，主体只有一行 `` `AXI_ASSIGN(out, in)``。

**需要观察的现象**：使用接口后，端口声明从「几十行」缩到「两行」，方向由 modport 自动给出。

**预期结果**：你能口头说出「为什么 `aw_valid` 在 Master 视角是 output、在 Slave 视角是 input」。本步为「源码阅读型实践」，无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：如果两个模块都声明成 `AXI_BUS.Master` 然后对接，会发生什么？

**答案**：两个 Master 都把 `aw_valid` 当输出、`aw_ready` 当输入，于是 `aw_valid` 没人接收、`aw_ready` 没人驱动，逻辑上不能完成握手；多数综合器/仿真器也会因「多个驱动源」报错或告警。这正是 modport 方向检查的价值——连错视角会直接暴露。

**练习 2**：为什么接口里要放 `typedef logic [AXI_ID_WIDTH-1:0] id_t;`，而不是每个信号都写 `logic [AXI_ID_WIDTH-1:0]`？

**答案**：一是让信号声明更短更可读（`id_t aw_id;`）；二是让「同一类信号共用同一种类型」这一约束显式化，改位宽时只改一处。这也是 u2-l1 强调的类型集中管理的延伸。

---

### 4.2 AXI_BUS：把五个通道封装进一个可综合接口

#### 4.2.1 概念说明

`AXI_BUS` 是本库最基础、**可综合**的 AXI4 接口。它把 AW/W/B/AR/R 五个通道的所有信号收进一个 bundle，并为这捆信号提供 `Master`、`Slave`、`Monitor` 三个 modport。绝大多数 RTL 模块（如 `axi_xbar`、`axi_demux`、`axi_join`）的 `_intf` 变体都用 `AXI_BUS.Slave` / `AXI_BUS.Master` 作为端口。

> 小贴士：本库的很多模块都有两个变体——一个用扁平 `req_t`/`resp_t` 结构体做端口（更现代、参数化更灵活），一个用 `AXI_BUS` 接口做端口（`_intf` 后缀）。两者功能等价，靠 u2-l4 将讲的 `AXI_ASSIGN` 宏互连。本讲聚焦 `AXI_BUS` 接口这一路。

#### 4.2.2 核心流程

`AXI_BUS` 内部的信号按五个通道分组，每根通道都遵循同一套「载荷信号 + `valid` + `ready`」的模式：

```
AW 通道: aw_id/aw_addr/aw_len/aw_size/aw_burst/aw_lock/aw_cache/aw_prot/
         aw_qos/aw_region/aw_atop/aw_user   + aw_valid + aw_ready   (主→从)
W  通道: w_data/w_strb/w_last/w_user        + w_valid  + w_ready    (主→从)
B  通道: b_id/b_resp/b_user                 + b_valid  + b_ready    (从→主)
AR 通道: ar_id/ar_addr/ar_len/.../ar_user   + ar_valid + ar_ready   (主→从)
R  通道: r_id/r_data/r_resp/r_last/r_user   + r_valid  + r_ready    (从→主)
```

三个 modport 的差别只在「每根信号是 input 还是 output」：

| modport | AW/W/AR 载荷 + valid | ready | B/R 载荷 + valid | 含义 |
| --- | --- | --- | --- | --- |
| `Master` | output | input | input | 我是主：发请求、收响应 |
| `Slave` | input | output | output | 我是从：收请求、发响应 |
| `Monitor` | input | input | input | 我是旁观者：只读不驱动 |

关键直觉：`Master` 与 `Slave` 的方向**逐根对调**；`Monitor` 把**所有**信号都设为 `input`，因此它不驱动任何信号，只观察——这正是 monitor / dumper / 比较器需要的视角。

#### 4.2.3 源码精读

三个 modport 的定义就值得逐行读一遍，它是「方向对调」的最直观教材：

[src/axi_intf.sv:85-91](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L85-L91) — `Master` modport：AW/W/AR 的载荷与 valid 都是 `output`，对应 ready 是 `input`；B/R 的载荷与 valid 是 `input`，b_ready/r_ready 是 `output`。

[src/axi_intf.sv:93-99](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L93-L99) — `Slave` modport：与 `Master` 逐根对调。注意一个易错点——`aw_atop`（原子操作编码）出现在 AW 通道里，u2-l1 讲过它是 6 位的 `axi_pkg::atop_t`，只挂在写地址上，AR 通道没有对应字段。

[src/axi_intf.sv:101-107](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L101-L107) — `Monitor` modport：**所有**信号一律 `input`，包括所有 `ready`。这是「只读旁观」的精确含义。

真实使用样本——`axi_join_intf` 把一个 `Slave` 端口原样连到一个 `Master` 端口：

[src/axi_join.sv:19-24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv#L19-L24) — 端口声明 `AXI_BUS.Slave in` 与 `AXI_BUS.Master out`，主体只有 `` `AXI_ASSIGN(out, in)``。注意此处没有 `clk`——`AXI_BUS` 是**不带时钟**的纯组合/可综合接口，时序由使用它的模块自己管。

#### 4.2.4 代码实践

**实践目标**：在已有测试台结构里，看出 `AXI_BUS`（无时钟）与稍后的 `AXI_BUS_DV`（带时钟）是如何分工的。

**操作步骤**：

1. 阅读 [test/tb_axi_modify_address.sv:59-80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_modify_address.sv#L59-L80)。
2. 注意它声明了**两种**接口：无时钟的 `AXI_BUS upstream/downstream ()`（接 DUT）和带时钟的 `AXI_BUS_DV upstream_dv/downstream_dv (.clk_i(clk))`（接随机主从驱动）。
3. 两者用 `` `AXI_ASSIGN(upstream, upstream_dv)`` 互连——一个三明治结构：`DV(带时钟/驱动)` ↔ `AXI_BUS(无时钟)` ↔ `DUT`。

**需要观察的现象**：`AXI_BUS` 实例化时小括号是空的 `()`，而 `AXI_BUS_DV` 实例化时小括号里必须给 `.clk_i(clk)`。

**预期结果**：你能解释「为什么驱动类和断言需要带时钟的 DV 接口，而 DUT 本身用不带时钟的 `AXI_BUS` 就够了」。本步为源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`Monitor` modport 把 `aw_ready` 也设成了 `input`，但 `Master`/`Slave` 里 `aw_ready` 是方向相反的。为什么 monitor 需要 `ready`？

**答案**：因为一次 AXI 握手必须 `valid && ready` 同时为高才算发生。monitor 若只看 `valid` 不看 `ready`，会把「主设备想发但还没被接收」的请求误判成已发生。同时读 `valid` 和 `ready`，才能精确捕获真正握手的那一拍。

**练习 2**：`AXI_BUS` 接口里为什么没有 `clk`？

**答案**：`AXI_BUS` 面向可综合 RTL，时钟属于模块级的全局资源，由顶层统一分发，不应耦合进「只是一捆线」的接口里。把时钟塞进接口会让它难以在不同时钟域（如 CDC 场景）复用。带时钟的版本另设为 `AXI_BUS_DV`，专供验证。

---

### 4.3 AXI_BUS_DV：给接口加上时钟与协议断言

#### 4.3.1 概念说明

`AXI_BUS_DV`（DV = Design Verification）几乎和 `AXI_BUS` 完全一样：同样的参数、同样的信号、同样的三个 modport。唯一的结构差别是它在接口参数列表后多了一个 `input logic clk_i` 端口。这个看似不起眼的改动带来两大能力：

1. **驱动类绑定**：测试台里的 driver / monitor 类可以把 `AXI_BUS_DV` 当作「虚接口」（virtual interface）引用，配合 `clk_i` 在时钟沿上精确地施加/采样信号——这是 u3-l1 将讲的 `axi_driver` 的工作方式。
2. **协议断言**：有了时钟，接口内部就能写 `assert property (@(posedge clk_i) ...)`，自动检查 AXI 协议规则，仿真一违例就报错。

因为带时钟和断言，`AXI_BUS_DV` 不可综合，仅用于仿真。

#### 4.3.2 核心流程

`AXI_BUS_DV` 的「加料」流程：

```
AXI_BUS (无时钟, 可综合)
   │  + input logic clk_i
   ▼
AXI_BUS_DV (带时钟)
   │  + assert property (@(posedge clk_i) ...)  对每个通道
   ▼
验证专用接口（驱动绑定 + 自动协议检查）
```

接口内挂的断言分两类：

- **单通道稳定性断言**：当 `valid && !ready`（即 pending，valid 高但还没握手）时，下一拍所有载荷信号和 `valid` 本身都必须保持不变（`$stable`）。这正是 u1-l3 讲的「valid 一旦拉高，握手前不可撤、载荷不可变」的协议要求。
- **地址通道 4 KiB 页断言**：一个突发不能跨越 4 KiB 页边界——首拍地址与末拍地址必须落在同一 4 KiB 页。这里用 u2-l2 讲过的 `axi_pkg::beat_addr(...)` 计算末拍地址来核对。

#### 4.3.3 源码精读

[src/axi_intf.sv:113-120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L113-L120) — `AXI_BUS_DV` 的声明，与 `AXI_BUS` 的参数完全相同，差别仅在参数列表后多出 `(input logic clk_i)`。这是 SV 语法：interface 可以带端口，`clk_i` 就是它的端口。

[src/axi_intf.sv:204-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L204-L220) — 断言区被 ``// pragma translate_off`` 与 `` `ifndef VERILATOR`` 双重包裹：前者让综合工具忽略这段，后者跳过不支持这些语法的 Verilator。注意 [src/axi_intf.sv:220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L220) 这条 `(aw_valid && !aw_ready |=> aw_valid)`——它要求 pending 期间 `aw_valid` 自身也不能掉，其余 `|=> $stable(...)` 则要求各载荷字段保持不变。

[src/axi_intf.sv:254-261](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L254-L261) — 4 KiB 页边界断言：用 `axi_pkg::beat_addr` 算出突发首拍（index 0）和末拍（index `aw_len`）地址，各右移 12 位（除以 4 KiB）后必须相等，否则 `$error` 报「AW burst crossing 4 KiB page boundary」。

真实使用样本——全库唯一真正用 `Monitor` modport 的模块 `axi_dumper_intf`：

[src/axi_dumper.sv:163-178](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L163-L178) — 端口声明 `AXI_BUS_DV.Monitor axi_bus`。注意它带了 `clk_i`、`rst_ni`，因为要在时钟沿上采样握手；并且因为是 `Monitor`，它**不驱动**总线任何信号，纯旁观。

[src/axi_dumper.sv:196-197](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L196-L197) — 用 `AXI_ASSIGN_TO_REQ` / `AXI_ASSIGN_TO_RESP` 把 `Monitor` 接口上的信号读进 `req`/`resp` 结构体（这两个宏是 u2-l4 的内容），再交给内部真正的 dumper 打印。

#### 4.3.4 代码实践

**实践目标**：仿照 `axi_dumper_intf`，写一个最小的 AW 通道 monitor——挂在 `AXI_BUS_DV.Monitor` 上，每当 AW 通道发生握手，就把这一拍的 `aw_id` 与 `aw_addr` 打印出来。

**操作步骤**：

1. 新建一个文件（**示例代码**，不是项目原有文件，请放到你自己的实验目录而非 `src/`），写入下面这个模块。它完全照搬 `axi_dumper_intf` 的端口骨架：带 `clk_i`/`rst_ni`，端口用 `AXI_BUS_DV.Monitor`。

```systemverilog
// 示例代码：最小 AW 通道 monitor
// 文件名建议：aw_monitor_intf.sv
`include "axi/assign.svh"

module aw_monitor_intf #(
  parameter int unsigned AXI_ID_WIDTH   = 32'd0,
  parameter int unsigned AXI_ADDR_WIDTH = 32'd0,
  parameter int unsigned AXI_DATA_WIDTH = 32'd0,
  parameter int unsigned AXI_USER_WIDTH = 32'd0
) (
  input  logic             clk_i,
  input  logic             rst_ni,
  AXI_BUS_DV.Monitor       axi_bus   // 关键：Monitor 视角，只读不驱动
);
  // 在时钟沿上采样：valid 与 ready 同高 = 一次 AW 握手
  always_ff @(posedge clk_i) begin
    if (rst_ni && axi_bus.aw_valid && axi_bus.aw_ready) begin
      $display("[aw_monitor] AW handshake: id=0x%0h addr=0x%0h",
               axi_bus.aw_id, axi_bus.aw_addr);
    end
  end
endmodule
```

2. 照搬 [test/tb_axi_modify_address.sv:50-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_modify_address.sv#L50-L58) 的写法，在某个已有测试台里声明一个 `AXI_BUS_DV` 并把 `clk` 接进 `.clk_i`：

```systemverilog
// 示例代码：在测试台里例化上面的 monitor
AXI_BUS_DV #(
  .AXI_ADDR_WIDTH (32),
  .AXI_DATA_WIDTH (64),
  .AXI_ID_WIDTH   (3),
  .AXI_USER_WIDTH (2)
) probe_dv (.clk_i(clk));          // 必须接时钟，否则 DV 接口无意义

aw_monitor_intf #(
  .AXI_ID_WIDTH(3), .AXI_ADDR_WIDTH(32),
  .AXI_DATA_WIDTH(64), .AXI_USER_WIDTH(2)
) i_probe (
  .clk_i    (clk),
  .rst_ni   (rst_n),
  .axi_bus  (probe_dv)             // Monitor 端口接到 DV 接口
);
// 再用 `AXI_ASSIGN(probe_dv, upstream) 把 probe_dv 挂到你想观察的那段总线上
```

3. 跑一次该测试台的仿真（参考 u1-l4 讲过的 `make sim-<tb>.log` 流程）。

**需要观察的现象**：每次 AW 通道真正握手（`aw_valid && aw_ready` 同高）的那个上升沿，标准输出会打印一行 `id` 和 `addr`；`pending` 期间（valid 高、ready 低）不会重复打印。

**预期结果**：打印的 AW 次数与该测试台发出的写事务数一致；若你故意在别处制造 `aw_valid` 抖动，`AXI_BUS_DV` 内置的 [src/axi_intf.sv:208-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L208-L220) 断言会先于你的 monitor 报错。

> 说明：本实践的精确运行结果取决于你接入哪段总线、发多少写事务，**待本地验证**。但模块骨架与端口写法直接取自项目真实的 `axi_dumper_intf`，是可用的。

#### 4.3.5 小练习与答案

**练习 1**：把上面 monitor 里的判断条件从 `aw_valid && aw_ready` 改成只看 `aw_valid`，会多打印什么？

**答案**：会额外打印那些「主设备已经拉高 `aw_valid` 但从设备还没回 `aw_ready`」的拍。也就是说，一个还没被接收的请求会被当成已发生的事务打印多次（pending 几拍就重复打几次）。这印证了练习 4.2.5 第 1 题的结论：monitor 必须同时看 `ready`。

**练习 2**：`AXI_BUS_DV` 里的断言为什么用 ``// pragma translate_off`` **和** `` `ifndef VERILATOR`` 两层包裹？

**答案**：两层分别针对两类工具——`pragma translate_off` 让综合工具（如 DC）跳过这段不可综合的断言；`` `ifndef VERILATOR`` 则因为 Verilator 对 `assert property` 这类语法支持有限/语义不同，需单独跳过。这是本库为兼容多种 EDA 工具而采用的典型双保险写法（u16-l3 会系统讨论）。

---

### 4.4 AXI_LITE / AXI_LITE_DV 与异步变体

#### 4.4.1 概念说明

`AXI_LITE` 是 AXI4-Lite 协议的接口：它去掉了所有突发、ID、QoS、region、atop、user 等字段，每个事务只搬「一个地址 + 一拍数据」，专门给寄存器配置、低速外设这类场景用（详见 u12 单元）。`AXI_LITE_DV` 则是它带时钟的验证版本。此外文件还定义了几个**异步**变体（`AXI_BUS_ASYNC`、`AXI_BUS_ASYNC_GRAY`、`AXI_LITE_ASYNC_GRAY`），用于时钟域跨越（CDC），它们的信号里多出 `writetoken`/`readpointer` 或 `wptr`/`rptr` 这类握手指针，留给 u8 单元细讲。

#### 4.4.2 核心流程

`AXI_LITE` 相对 `AXI_BUS` 的「瘦身」对照：

| 通道 | AXI_BUS 载荷 | AXI_LITE 载荷 |
| --- | --- | --- |
| AW | id/addr/len/size/burst/lock/cache/prot/qos/region/atop/user | addr/prot |
| W | data/strb/last/user | data/strb |
| B | id/resp/user | resp |
| AR | id/addr/len/size/burst/lock/cache/prot/qos/region/user | addr/prot |
| R | id/data/resp/last/user | data/resp |

异步 Gray 变体（`*_ASYNC_GRAY`）则把每个通道的载荷打包成一个数组（深度 `2**LOG_DEPTH`），再配一对 `wptr`/`rptr` 指针，本质是一个用 Gray 编码指针同步的 FIFO 接口契约。

#### 4.4.3 源码精读

[src/axi_intf.sv:410-425](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L410-L425) — `AXI_LITE` 声明，参数只剩 `AXI_ADDR_WIDTH` 与 `AXI_DATA_WIDTH`（没有 ID/USER），AW 通道只剩 `aw_addr`/`aw_prot`/`aw_valid`/`aw_ready`。

[src/axi_intf.sv:446-468](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L446-L468) — `AXI_LITE` 同样有 `Master`/`Slave`/`Monitor` 三个 modport，方向规则与 `AXI_BUS` 完全一致，只是信号更少。

[src/axi_intf.sv:474-479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L474-L479) — `AXI_LITE_DV`，结构与 `AXI_BUS_DV` 同构：多一个 `clk_i` 端口，但**注意**它没有像 `AXI_BUS_DV` 那样内嵌断言——Lite 事务恒为单拍、无突发，省去了 4 KiB 页与稳定性断言的必要（载荷只有一拍，pending 期间仍需稳定，但本库未在此处加断言）。

[src/axi_intf.sv:359-390](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv#L359-L390) — `AXI_BUS_ASYNC_GRAY`：每个通道用 `AXI_TYPEDEF_*` 宏（u2-l4）声明一个通道结构体，再声明深度为 `2**LOG_DEPTH` 的数组 `aw_data[...]` 与一对 `aw_wptr`/`aw_rptr`。这就是 Gray CDC FIFO 的接口契约，细节留待 u8-l1。

#### 4.4.4 代码实践

**实践目标**：对比 Lite 与完整 AXI4 接口的信号集合，建立「何时用哪个」的直觉。

**操作步骤**：

1. 数一下 `AXI_LITE` 的信号总数（约 18 根）与 `AXI_BUS`（约 43 根）的差距。
2. 在仓库里搜 `AXI_LITE_DV` 的使用，例如 [test/tb_axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv)（u3-l3 会精读）。
3. 思考：一个纯寄存器配置端口，用 `AXI_BUS` 会让哪些信号永远闲置？

**需要观察的现象**：Lite 接口省掉了所有与「多拍突发」「多 ID 并发」相关的信号。

**预期结果**：你能说出「寄存器配置类从端适合用 `AXI_LITE`，而带 DMA / cache 的主端口必须用 `AXI_BUS`」。本步为源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：`AXI_LITE` 的 B 通道只有 `b_resp`，没有 `b_id`。为什么 Lite 不需要 `b_id`？

**答案**：AXI4-Lite 规定每次事务只能搬一拍数据、且不允许不同 ID 的事务交错（实质上串行处理），因此响应无需用 ID 来配对——返回的 B/R 天然属于「当前唯一在途」的那个事务。完整 AXI4 允许多 ID 并发，才需要 `b_id`/`r_id` 做配对。

**练习 2**：`AXI_BUS_ASYNC_GRAY` 里为什么每个通道是「一个结构体数组」而不是「多根独立信号」？

**答案**：跨时钟域要用 Gray 编码的读/写指针同步一整「拍」载荷，把一拍载荷先打包成一个 struct，再把若干拍存成数组（FIFO 存储体），最后只同步指针——这样指针位数少、Gray 同步成本低。这是 u8 单元 CDC FIFO 的核心结构。

---

## 5. 综合实践

把本讲知识串起来，做一次「接口选型 + 接线 + 观察」的小任务。

**任务**：为一个假想的 DUT（任意一个 `_intf` 模块，比如 [src/axi_join.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv) 里的 `axi_join_intf`）搭一个最小测试台骨架，要求：

1. 选对接口类型——DUT 端口用 `AXI_BUS.Slave`/`AXI_BUS.Master`（可综合、无时钟），驱动/观察侧用 `AXI_BUS_DV`（带时钟）。
2. 用 u1-l4 讲过的 `clk_rst_gen` 产生 `clk`/`rst_n`，并把 `clk` 接到 `AXI_BUS_DV` 的 `.clk_i`。
3. 用 `` `AXI_ASSIGN``（u2-l4）在 `AXI_BUS_DV` 与 `AXI_BUS` 之间搭桥，形成 `DV ↔ AXI_BUS ↔ DUT` 三明治。
4. 在 DUT 的 slave 侧再挂一个你在 4.3.4 写的 `aw_monitor_intf`（`AXI_BUS_DV.Monitor`），用 `AXI_ASSIGN` 把它连到同一段总线。
5. 跑仿真，确认 monitor 打印的 AW 握手与驱动发出的事务数一致；并确认 `AXI_BUS_DV` 的内置断言没有触发（即 `Errors: 0`）。

**评判标准**：

- 接口类型选对（DUT 用无时钟 `AXI_BUS`、验证侧用 `AXI_BUS_DV` 且接了 `clk_i`）。
- modport 视角用对（DUT slave 侧接 `Master` 驱动、`Monitor` 只读）。
- 能解释为什么不能把 `AXI_BUS_DV` 直接接到 DUT（DUT 是可综合 RTL，不应耦合时钟进接口；且 DV 接口带断言/不可综合）。

> 精确仿真通过情况取决于你接入的 DUT 与激励，**待本地验证**。

## 6. 本讲小结

- `interface` 把 AXI4 五通道的几十根信号收成一捆，`modport` 为 `Master`/`Slave`/`Monitor` 预设方向，消除了裸端口列表的冗长与连错方向的风险。
- `AXI_BUS` 是不带时钟、**可综合**的基础接口；`Master` 与 `Slave` 逐根对调方向，`Monitor` 把所有信号设为只读 `input`。
- `AXI_BUS_DV` 在 `AXI_BUS` 上加一个 `clk_i` 端口，从而能挂 `assert property` 协议断言（pending 期间信号稳定、突发不跨 4 KiB 页），专供仿真、不可综合。
- 写 monitor/dumper 时端口用 `AXI_BUS_DV.Monitor`，需自带 `clk_i`/`rst_ni`，并在 `valid && ready` 同时为高时才认为一次握手发生——这是采样 AXI 事务的通用准则。
- `AXI_LITE` / `AXI_LITE_DV` 是去掉 ID/突发等字段的精简版；`AXI_BUS_ASYNC_GRAY` 等异步变体用结构体数组 + `wptr`/`rptr` 表达 CDC FIFO 契约，细节留待 u8。
- 测试台的典型三明治结构：带时钟的 `AXI_BUS_DV`（驱动/观察）↔ 无时钟的 `AXI_BUS` ↔ 可综合 DUT，两层之间用 `AXI_ASSIGN` 互连。

## 7. 下一步学习建议

- **紧接着学 u2-l4（typedef / assign / port 宏体系）**：本讲反复出现的 `` `AXI_ASSIGN``、`` `AXI_ASSIGN_TO_REQ``、`` `AXI_TYPEDEF_*`` 都来自 `include/axi/` 下的三大宏文件，搞懂它们你就能自己声明 `req_t`/`resp_t` 并在接口与结构体之间搬数据。
- **随后进入 u3 单元（仿真与验证基础设施）**：u3-l1 会讲 `axi_test.sv` 里的 driver 类如何把一个 `AXI_BUS_DV` 虚接口驱动起来，本讲的 DV 接口正是它的舞台。
- **想先看接口被「用起来」**：可提前翻阅 [src/axi_dumper.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv)（Monitor 视角的完整范本）和 [test/tb_axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv)（Lite 接口的完整测试台）。
- **异步变体留到 u8**：届时结合 `axi_cdc` 一起理解 Gray 指针同步的工程实现。
