# AXI 部分事务与突发传输

## 1. 本讲目标

学完本讲，你应当能够：

- 区分 `apply_*`（驱动某一侧）与 `expect_*`（接受并校验另一侧）两类「部分事务」过程，并说出每个过程里 `ms` / `sm` 谁是 `in`、谁是 `out`。
- 用 `axi_apply_aw/ar` + `axi_expect_aw/ar` 拆解 AXI 地址通道，用 `axi_apply_wd_*` / `axi_expect_wd_*` 拆解写数据通道，用 `axi_apply_rresp_*` / `axi_expect_rresp_*` 拆解读数据通道，用 `axi_apply_bresp` / `axi_expect_bresp` 拆解写响应通道。
- 理解突发参数 `Beats`、`DataStart`、`DataIncr`、`WstrbFirst/Last` 的含义，以及 `AxLen`（协议字段，= 拍数 − 1）与 `Beats`（拍数本身）的换算坑。
- 理解 `VldLowCycles`（生产者节流）与 `RdyLowCycles`（消费者背压）两种节流机制如何用来测试 DUT 在非理想吞吐下的鲁棒性。
- 用两个并发进程（一个扮 master、一个扮 slave）拼出一次完整的 INCR 突发读。

## 2. 前置知识

本讲建立在 [u5-l2](u5-l2-axi-single-transactions.md) 之上，默认你已经掌握：

- `axi_ms_r` / `axi_sm_r` 两条 record：主机驱动的信号进 `ms`，从机驱动的信号进 `sm`（见 [u5-l1](u5-l1-axi-types-and-init.md)）。
- `axi_single_write/read/expect` 是「一次调用干完一件事」的高层过程，它默认对端是理想 slave/master（立即 ready、回 OKAY）。
- `StdlvCompareStdlv`、`StdlvCompareInt`、`StdlCompare` 失败时打印 `###ERROR###` 前缀的消息，`severity error` 不中断仿真，由 CI 的 `run_check_errors "###ERROR###"` 统一捕获。
- AXI 有五个独立通道：AW（写地址）、W（写数据）、B（写响应）、AR（读地址）、R（读数据），每个通道都是独立的 valid/ready 握手。

**为什么还需要本讲？** `axi_single_*` 只能产生「单拍（len=0）」事务，且把 master 与 slave 的行为绑死在一个过程里——它假设 slave 总是立刻 ready、总是回 OKAY。现实里你要验证的 DUT 可能是一个真正的 AXI slave IP，它会有自己的时序（延迟 ready、突发、回 SLVERR……），也可能是一个真正的 AXI master，你需要一个 slave BFM 去喂它。这时就必须把一次 AXI 事务**按通道拆开**，master 侧和 slave 侧**各自独立编写、通过并发进程对拍**。这就是 `axi_apply_*` / `axi_expect_*` 家族存在的理由。

## 3. 本讲源码地图

本讲全部内容集中在同一个文件：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd) | AXI BFM 包。本讲聚焦其中的「Partial Transactions」段落（地址 / 写数据 / 读响应 / 写响应的 apply 与 expect 过程）。 |

复用到的底层（不在本讲精读，但会被引用）：

| 文件 | 作用 |
| --- | --- |
| hdl/psi_tb_compare_pkg.vhd | 提供 `StdlvCompareStdlv` / `StdlvCompareInt` / `StdlCompare`，是所有 `expect_*` 的校验底座。 |

> 说明：仓库目前没有 AXI 的 testbench，AXI 包也未注册进 `sim/config.tcl` 的 CI 编译列表（见 [u1-l2](u1-l2-repository-structure.md)）。因此本讲的实践属于「自己写一个最小 TB 并在本机跑」的类型，凡涉及运行结果的环节均标注「待本地验证」。

## 4. 核心概念与源码讲解

### 4.1 apply 与 expect 的分工模型，以及节流机制

#### 4.1.1 概念说明

`axi_single_*` 是「黑箱」：一次调用里既驱动 master、又隐式假设 slave 的理想行为。`axi_apply_*` / `axi_expect_*` 则是把黑箱拆成「每个通道一次握手」的原子积木。理解这一族过程，关键在于建立下面这个**分工模型**：

- **`apply_*`（驱动方）**：主动把某一侧的信号摆成「我要发起这个通道的传输」，然后等对端在那个通道上 ready。握手成功后调用对应的 `init` 把信号收回到空闲（撤销 valid）。它**不校验**任何东西。
- **`expect_*`（校验方）**：在那个通道上**抬高自己的 ready**去**接受**对端发起的传输，握手成功后用 `StdlvCompare*` 校验对端实际送来的内容是否符合预期。它**不驱动**数据，只驱动 ready。

每一个 AXI 通道都恰好有一对互补的 `apply` / `expect`：一个由该通道的「生产者」一侧调用，另一个由「消费者」一侧调用。谁是生产者、谁是消费者，取决于通道方向：

| 通道 | 生产者（驱动数据/valid，用 `apply`） | 消费者（驱动 ready，用 `expect`） |
| --- | --- | --- |
| AW / AR（地址） | **master**（`axi_apply_aw/ar`） | **slave**（`axi_expect_aw/ar`） |
| W（写数据） | **master**（`axi_apply_wd_*`） | **slave**（`axi_expect_wd_*`） |
| R（读数据） | **slave**（`axi_apply_rresp_*`） | **master**（`axi_expect_rresp_*`） |
| B（写响应） | **slave**（`axi_apply_bresp`） | **master**（`axi_expect_bresp`） |

记住这个表，你就记住了所有过程的参数方向：**`apply` 过程的 `apply` 方信号是 `out`；`expect` 过程的 `expect` 方信号是 `out`**。例如 `axi_apply_aw` 里 `ms` 是 `out`、`sm` 是 `in`；而 `axi_apply_bresp` 因为响应由 slave 驱动，所以 `sm` 是 `out`、`ms` 是 `in`——同样是 `apply`，方向却相反。

由于每个 `apply_*` / `expect_*` 只阻塞在**一个通道的一次握手**上，要完成一次完整事务，必须用**两个并发进程**对拍：master 进程依次调用 master 侧的若干过程，slave 进程依次调用 slave 侧的若干过程，二者在共享的 `ms` / `sm` 信号上自然 rendezvous。这与 `axi_single_*`（单进程、master 独角戏）是根本不同的编程模型。

#### 4.1.2 核心流程

一次「master 发起、slave 响应」的写事务，对拍流程如下（两进程并发，左列与右列同步推进）：

```
master 进程                          slave 进程
-----------                          -----------
axi_apply_aw(addr,...)               axi_expect_aw(addr,...)   -- AW 通道握手 + slave 校验地址
axi_apply_wd_burst(...)              axi_expect_wd_burst(...)  -- W  通道握手 + slave 校验数据
axi_expect_bresp(xRESP_OKAY_c,...)   axi_apply_bresp(...)      -- B  通道握手 + master 校验响应
```

每个 `apply_*` / `expect_*` 内部都遵循同一个握手骨架：

```
摆信号（驱动方摆 valid+data；校验方摆 ready）
wait until rising_edge(aclk) and <对端的 ready/valid> = '1'   -- 命中一个 valid&ready 同时为高的上升沿
（校验方在此处做 StdlvCompare* 比较）
撤销（驱动方调用 init 撤销 valid；校验方把 ready 拉回 '0'）
```

**节流机制**是这一族过程相对 `axi_single_*` 的另一个关键增强。只有携带数据的「突发」通道（`wd_burst`、`rresp_burst`）才有节流参数，且两侧名字不同：

- `VldLowCycles`（出现在 `apply_wd_burst` / `apply_rresp_burst`）：**生产者**在相邻两拍之间额外插入 N 个 `valid='0'` 的空闲周期——模拟生产者「拿不出数据」。
- `RdyLowCycles`（出现在 `expect_wd_burst` / `expect_rresp_burst`）：**消费者**在相邻两拍之间额外插入 N 个 `ready='0'` 的背压周期——模拟消费者「吃不下数据」，迫使生产者把 valid 顶住等待。

二者都用来给 DUT 制造非理想的吞吐场景，测试其握手状态机是否正确处理 valid 等待 ready、ready 等待 valid 的情况。注意地址通道与 B 通道没有节流参数——它们本来就是单拍。

#### 4.1.3 源码精读

`apply` 与 `expect` 的方向差异，最直观地体现在过程头的参数模式上。对比两个同为 `apply`、却驱动不同侧的过程头：

[axi_apply_aw（master 侧，ms 是 out）: hdl/psi_tb_axi_pkg.vhd:L718-L724](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L718-L724) —— 地址由 master 发起，所以 `signal ms : out axi_ms_r`。

[axi_apply_bresp（slave 侧，sm 是 out）: hdl/psi_tb_axi_pkg.vhd:L1022-L1025](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1022-L1025) —— 写响应由 slave 发起，所以 `signal sm : out axi_sm_r`。

`apply` 的握手 + 回收骨架，以 `axi_apply_ar` 为例：

[axi_apply_ar 实现: hdl/psi_tb_axi_pkg.vhd:L735-L750](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L735-L750) —— 关键三步：先摆 `ms.arvalid <= '1'` 并填好地址/控制字段；再 `wait until rising_edge(aclk) and sm.arready = '1'` 等到 slave 抬高 ready 的那个上升沿；最后调用 [axi_master_init(ms)](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L467-L500) 把整束 master 信号归零、撤销 arvalid，让总线回到空闲。`init` 就是这个家族里通用的「回空闲」原语。

`expect` 的「抬 ready → 等对端 → 校验 → 收 ready」骨架，以 `axi_expect_ar` 为例：

[axi_expect_ar 实现: hdl/psi_tb_axi_pkg.vhd:L878-L893](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L878-L893) —— 先 `sm.arready <= '1'` 表示 slave 准备好收地址，`wait until rising_edge(aclk) and ms.arvalid = '1'` 等到 master 把地址送上来，随后做四项 `StdlvCompare*` 校验，最后 `sm.arready <= '0'` 收回 ready。

> 注意一个共性：`apply_*` 在握手后调用 `axi_master_init` / `axi_slave_init`（取决于驱动方）来回收信号；`expect_*` 不调 init，只把自己的那一根 ready 拉回 `'0'`。这是因为 expect 方只动过 ready，其余信号它无权改写。

#### 4.1.4 代码实践

**实践目标**：亲手把 apply/expect 的方向模型内化为一张表。

**操作步骤**：

1. 打开 [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd) 的「Partial Transactions」段落（约 L179 起）。
2. 只看每个过程头的参数模式（`signal ms : in/out`、`signal sm : in/out`），不看实现。
3. 为下列过程各判断一句：「谁驱动？」
   - `axi_apply_aw`、`axi_expect_aw`
   - `axi_apply_wd_burst`、`axi_expect_wd_burst`
   - `axi_apply_rresp_burst`、`axi_expect_rresp_burst`
   - `axi_apply_bresp`、`axi_expect_bresp`

**预期结果**：8 个过程的方向应当与 4.1.1 的表格完全吻合——`apply` 一律驱动对应通道的生产者侧（`ms` 或 `sm` 看通道而定），`expect` 一律只把消费者侧的 ready 当 `out`。如果你判断出的方向与表格不一致，回头核对参数列表里的 `in`/`out` 关键字。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_apply_bresp` 的 `sm` 是 `out` 而 `axi_apply_aw` 的 `sm` 是 `in`？

**参考答案**：`apply` 永远驱动「它负责发起的那个通道」的生产者侧。AW 通道由 master 发起，slave 只是被动提供 `awready`，所以 `axi_apply_aw` 驱动 `ms`（out）、读 `sm`（in）。B 通道由 slave 发起（写响应是 slave 给 master 的），所以 `axi_apply_bresp` 驱动 `sm`（out）、读 `ms`（in）。方向由「通道的生产者是谁」决定，不由过程名决定。

**练习 2**：`VldLowCycles` 与 `RdyLowCycles` 分别出现在哪一侧的过程里？为什么？

**参考答案**：`VldLowCycles` 出现在 `apply_wd_burst` / `apply_rresp_burst`（生产者侧），它把生产者的 `valid` 周期性拉低，模拟「拿不出数据」；`RdyLowCycles` 出现在 `expect_wd_burst` / `expect_rresp_burst`（消费者侧），它把消费者的 `ready` 周期性拉低，模拟背压。两者都只存在于携带数据的突发通道，地址与 B 通道是单拍、没有节流参数。

---

### 4.2 地址通道：axi_apply_aw / axi_apply_ar 与 axi_expect_aw / axi_expect_ar

#### 4.2.1 概念说明

地址通道（AW/AR）携带的是「这次事务的说明书」：起始地址 `AxAddr`、每拍字节数 `AxSize`、突发长度 `AxLen`、突发类型 `AxBurst`。`apply` 侧把这份说明书摆上线，`expect` 侧收下并逐字段核对。

需要特别强调两个坑：

1. **`AxLen` 不是拍数，而是 AXI 协议的 AxLEN 字段，其值 = 拍数 − 1**。一个 4 拍突发，`AxLen` 要填 `3`。这与你随后在数据通道里传给 `*_burst` 的 `Beats := 4`（拍数本身）是两套数，拼装一次完整突发时必须自己换算。
2. `axi_apply_aw/ar` 只设置 `axaddr/axvalid/axlen/axburst/axsize` 五个字段，**不碰** `axid/axlock/axcache/axprot/axqos/axregion/axuser`——后者保持 `axi_master_init` 留下的全 0 默认值。如果你的 DUT 依赖这些字段，要么先手动赋值再调 `apply`，要么接受它们为 0。

#### 4.2.2 核心流程

```
apply_aw/ar(AxAddr, AxSize, AxLen, AxBurst, ms, sm, aclk):
    ms.axaddr  <= AxAddr
    ms.axvalid <= '1'
    ms.axlen   <= AxLen          -- 直接当 AxLEN 字段写入，不做 -1 换算
    ms.axburst <= AxBurst
    ms.axsize  <= AxSize
    wait until rising_edge(aclk) and sm.axready = '1'
    axi_master_init(ms)          -- 撤销 axvalid，回空闲

expect_aw/ar(AxAddr, AxSize, AxLen, AxBurst, ms, sm, aclk):
    sm.axready <= '1'
    wait until rising_edge(aclk) and ms.axvalid = '1'
    StdlvCompareInt (AxAddr, ms.axaddr, "wrong AxADDR", IsSigned=>false)
    StdlvCompareStdlv(AxSize, ms.axsize, "wrong AxSIZE")
    StdlvCompareInt (AxLen,  ms.axlen,  "wrong AxLEN",  IsSigned=>false)
    StdlvCompareStdlv(AxBurst, ms.axburst, "wrong AxBURST")
    sm.axready <= '0'
```

`AxSize` 与字节数的关系由协议规定：

\[
\text{bytes\_per\_beat} = 2^{\text{AxSize}}
\]

`AxSize = AxSIZE_4_c ("010" = 2)` 即每拍 4 字节，对应 32 位数据总线。常用常量见 [u5-l1](u5-l1-axi-types-and-init.md)。

#### 4.2.3 源码精读

[axi_apply_aw 实现: hdl/psi_tb_axi_pkg.vhd:L718-L733](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L718-L733) —— 注意它把入参 `AxAddr`、`AxLen` 直接 `to_unsigned` 写进 `ms.awaddr` / `ms.awlen`，**没有做 −1 换算**：你传 `AxLen => 3`，线上 `awlen` 就是 `3`（即 4 拍突发）。

[axi_expect_aw 实现: hdl/psi_tb_axi_pkg.vhd:L861-L876](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L861-L876) —— 四项比较里，地址与长度的 `StdlvCompareInt(..., false)` 显式传 `IsSigned => false`，即按**无符号**整数解释向量（地址、长度本来就该是无符号的）；`AxSize` 与 `AxBurst` 是逐位向量比较（`StdlvCompareStdlv`）。任何一项不符都会打印 `###ERROR###` 消息并被 CI 捕获。

[axi_apply_ar 实现: hdl/psi_tb_axi_pkg.vhd:L735-L750](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L735-L750) 与 [axi_expect_ar 实现: hdl/psi_tb_axi_pkg.vhd:L878-L893](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L878-L893) —— AR 通道与 AW 通道逐行同构，只是把 `aw*` 换成 `ar*`。

#### 4.2.4 代码实践

**实践目标**：体会 `AxLen` 与 `Beats` 的换算坑，以及 `apply` 相对 `axi_single_*` 省略了哪些字段。

**操作步骤**：

1. 打开 [axi_single_write 的地址段: hdl/psi_tb_axi_pkg.vhd:L526-L535](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L526-L535)，对比 [axi_apply_aw: L718-L733](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L718-L733)。
2. 列出 `axi_single_write` 设置了而 `axi_apply_aw` 没设置的 master 字段。
3. 设想一次 8 拍突发写：你会给 `axi_apply_aw` 的 `AxLen` 传几？给 `axi_apply_wd_burst` 的 `Beats` 传几？

**预期结果**：第 2 步应发现 `axid/awlock/awcache/awprot/awqos/awregion/awuser` 在 `single_write` 里被显式置 0，而 `apply_aw` 完全没碰它们（依赖 init 的残留默认值）。第 3 步：`AxLen => 7`，`Beats => 8`。

#### 4.2.5 小练习与答案

**练习 1**：你在 `expect_aw` 里把 `AxLen` 期望值写成了 `4`，但 master 实际发的是 5 拍突发（`AxLen=4`）。请问 `expect_aw` 会不会报错？

**参考答案**：不会报错——`AxLen=4` 恰好就是 5 拍突发的正确 AxLEN 字段值（拍数 − 1 = 4），所以期望与实际一致，校验通过。容易出错的是反过来：你以为「5 拍」就要期望 `AxLen=5`，那才会误报。

**练习 2**：为什么 `expect_aw` 里地址和长度用 `StdlvCompareInt(..., false)` 而不是默认的 `IsSigned => true`？

**参考答案**：地址和突发长度都是无符号量。若用默认的 `IsSigned => true`，超过 31 位的高位地址会被当成负数解释，导致误报。显式传 `false` 让 `StdlvCompareInt` 按无符号整数比较（详见 [u3-l1](u3-l1-compare-basic.md) 对 `IsSigned` 的讨论）。

---

### 4.3 写数据通道：axi_apply_wd_* 与 axi_expect_wd_*

#### 4.3.1 概念说明

W 通道承载写数据，每拍带一个 `wstrb`（写掩码，每位对应 8 位数据）和一根 `wlast`（标记突发最后一拍）。这一族有 single 与 burst 两档：

- `axi_apply_wd_single` / `axi_expect_wd_single`：单拍，`wlast` 固定为 1。
- `axi_apply_wd_burst` / `axi_expect_wd_burst`：多拍，由 `Beats` 决定拍数，数据按 `DataStart` 起步、每拍加 `DataIncr`。

burst 各有两个重载：`natural` 重载（数据是普通整数，受 32 位 `natural` 范围限制）与 `string` 重载（数据以十进制/十六进制字符串给出，经 `decimal/hex_string_to_signed` 解析，支持 >32 位，见 [u5-l1](u5-l1-axi-types-and-init.md)）。

`WstrbFirst` 与 `WstrbLast` 分别指定首拍与末拍的写掩码；**中间拍的掩码恒为全 1**（`to_signed(-1, ...)`，即所有字节都写）。这隐含一个约定：用 burst 助手生成的写突发，中间拍不允许部分写——部分写只能出现在首拍或末拍。

#### 4.3.2 核心流程

`apply_wd_burst`（master 侧，生产者）：

```
ms.wvalid <= '1'
DataCnt := DataStart
for beat in 1..Beats loop
    if beat == Beats  -> ms.wlast<='1'; ms.wstrb<=WstrbLast
    elif beat == 1    ->                 ms.wstrb<=WstrbFirst
    else              ->                 ms.wstrb<=全1
    ms.wdata <= DataCnt
    wait until rising_edge(aclk) and sm.wready='1'      -- 这一拍被 slave 收下
    DataCnt := DataCnt + DataIncr
    if beat != Beats:                                   -- 非末拍后插节流
        for lc in 1..VldLowCycles:  ms.wvalid<='0'; wait until rising_edge(aclk)
        ms.wvalid <= '1'
axi_master_init(ms)
```

`expect_wd_burst`（slave 侧，消费者）结构对称，但有三点不同：① 开头是 `sm.wready <= '1'`；② 节流参数叫 `RdyLowCycles`，插入的是 `sm.wready <= '0'`；③ 每拍收下后做校验——`wlast`、`wstrb`、以及按字节比对 `wdata`（仅比对 `wstrb` 为 1 的字节）。

#### 4.3.3 源码精读

[axi_apply_wd_burst（natural 重载）: hdl/psi_tb_axi_pkg.vhd:L767-L806](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L767-L806) —— 关注三点：L781–L790 的 `wstrb` 三分支（首/末/中）；L792–L795 的「摆数据 → 等 wready → 计数器自增」；L797–L803 的 `VldLowCycles` 节流（只在非末拍后插入，且把 `wvalid` 拉低 N 个整周期再拉回）。末尾 [axi_master_init(ms)](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L467-L500) 收回总线。

[axi_apply_wd_burst（string 重载）: hdl/psi_tb_axi_pkg.vhd:L808-L859](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L808-L859) —— 与 natural 重载的循环结构完全一致，差别只在数据用 `signed` 计数器、起始值与增量由字符串按 `Base`（10/16）解析，从而突破 32 位。

[axi_expect_wd_burst（natural 重载）: hdl/psi_tb_axi_pkg.vhd:L915-L961](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L915-L961) —— L932–L941 校验每拍的 `wlast`/`wstrb`；L944–L949 按字节比对 `wdata`；L952–L958 是消费者的 `RdyLowCycles` 背压。

> **源码阅读观察（待本地验证）**：`axi_expect_wd_single` 与 `axi_expect_wd_burst` 里按字节比对数据时，切片写成了 `DataInt_c(byte * 8 - 1 downto byte * 8)`（见 [L907](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L907) 与 [L947](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L947)）。对 `byte=0`，这是 `(-1 downto 0)`；对 `byte=1`，这是 `(7 downto 8)`——都是左界 < 右界的降序空范围，对应的逐字节 `StdlvCompareStdlv` 在多数仿真器里对空向量比较恒为真，相当于这一路数据校验「不生效」。`wstrb` 与 `wlast` 的校验不受影响、仍然有效；读数据通道 `expect_rresp_burst` 用的是整向量比较、也不受影响。读到这段时请意识到这个现象，并在你自己的仿真器上验证一次，不要假定写数据内容已被自动比对。

#### 4.3.4 代码实践

**实践目标**：把 `VldLowCycles` 节流的波形在脑子里画出来。

**操作步骤**：

1. 精读 [axi_apply_wd_burst L797-L803](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L797-L803) 的节流内层循环。
2. 假设 `Beats => 4`、`VldLowCycles => 2`、slave 端 `wready` 恒为 1，画出 `wvalid`、`wlast`、`wdata` 在 8 个时钟周期内的时序图。
3. 标出哪些周期是「有效数据拍」、哪些是 `wvalid='0'` 的节流拍。

**预期结果**：第 1 拍 `wdata=DataStart, wvalid=1`；之后插 2 个 `wvalid=0` 周期；第 2 拍 `wdata=DataStart+Incr, wvalid=1`；再插 2 个节流周期……末拍（第 4 拍）`wlast=1`，其后**不再**插节流（`if not (beat = Beats)` 保护）。共约 4 + 3×2 = 10 个上升沿完成 4 拍。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：用 `axi_apply_wd_burst` 产生 `Beats=>4` 的写突发，想让第 1 拍只写最低字节、其余拍全写，应该怎么传 `WstrbFirst` 和 `WstrbLast`？

**参考答案**：`WstrbFirst => "0001"`（假设 32 位总线，4 字节，最低字节有效），`WstrbLast => "1111"`。中间拍由过程自动填全 1，无需也无法指定。注意中间拍不允许部分写。

**练习 2**：为什么 `apply_wd_burst` 用 `VldLowCycles`，而 `expect_wd_burst` 用 `RdyLowCycles`？

**参考答案**：apply 是生产者（master），节流的方式是把生产者的 `wvalid` 拉低，故叫 Vld Low；expect 是消费者（slave），节流的方式是把消费者的 `wready` 拉低（背压），故叫 Rdy Low。两者测试的是 DUT 同一握手状态机的不同分支：valid 等 ready、ready 等 valid。

---

### 4.4 读数据通道：axi_apply_rresp_* 与 axi_expect_rresp_*

#### 4.4.1 概念说明

R 通道承载读返回数据，每拍带 `rdata`、`rresp`、`rlast`。与 W 通道镜像，但有一个 AXI 协议层面的重要差别：**响应码 `rresp` 是逐拍的**，但 AXI 规定一次突发里只有最后一拍携带错误响应、前面所有拍应当是 OKAY。`psi_tb` 的 `expect_rresp_burst` 正是按这条约定设计的——你只需传一个 `Response` 值，过程会自动区分：

- 若 `Response = xRESP_OKAY_c`：**每一拍**都校验 `rresp` 必须为 OKAY。
- 若 `Response ≠ OKAY`（如 `xRESP_SLVERR_c`）：**只在最后一拍**校验 `rresp` 等于该错误码，前面几拍不校验响应（默认按 AXI 约定为 OKAY）。

`expect_rresp_single` 与 `expect_rresp_burst` 还提供 `IgnoreData` / `IgnoreResponse` 两个布尔开关，让你跳过数据或响应的比对——在读「只关心有没有回来、不关心具体值」的场景下很有用。

#### 4.4.2 核心流程

`apply_rresp_burst`（slave 侧，生产者）：

```
sm.rvalid <= '1';  sm.rlast <= '0';  sm.rresp <= Response
DataCnt := DataStart
for beat in 1..Beats loop
    if beat == Beats -> sm.rlast <= '1'
    sm.rdata <= DataCnt
    wait until rising_edge(aclk) and ms.rready='1'         -- 被 master 收下
    DataCnt := DataCnt + DataIncr
    if beat != Beats:                                       -- 非末拍后插节流
        for lc in 1..VldLowCycles:  sm.rvalid<='0'; wait until rising_edge(aclk)
        sm.rvalid <= '1'
axi_slave_init(sm)
```

`expect_rresp_burst`（master 侧，消费者）：开头 `ms.rready <= '1'`；每拍等 `sm.rvalid='1'` 后校验 `rlast`、按整向量比对 `rdata`、并按上面的规则校验 `rresp`；节流用 `RdyLowCycles` 把 `ms.rready` 拉低。

#### 4.4.3 源码精读

[axi_apply_rresp_burst（natural 重载）: hdl/psi_tb_axi_pkg.vhd:L1059-L1094](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1059-L1094) —— 注意 L1073 把 `sm.rresp <= Response` 设在循环之外：**整个突发的响应码是同一个常量**；L1076–L1077 只在末拍抬 `sm.rlast`；L1085–L1091 是生产者的 `VldLowCycles` 节流。

[axi_expect_rresp_burst（natural 重载）: hdl/psi_tb_axi_pkg.vhd:L1165-L1212](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1165-L1212) —— 重点看响应校验的两条分支：L1186–L1188 在「末拍且 `Response ≠ OKAY`」时比对错误响应；L1199–L1201 在「`Response = OKAY`」时对**每一拍**都比对 OKAY。这就是「错误只在末拍」的协议建模。L1193–L1195 用 `StdlvCompareStdlv(DataStdlv_v, sm.rdata, ...)` 做**整向量**数据比对（与 wd 通道的逐字节切片不同，这里数据校验是完全有效的）。

[axi_apply_rresp_single: hdl/psi_tb_axi_pkg.vhd:L1044-L1057](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1044-L1057) 与 [axi_expect_rresp_single: hdl/psi_tb_axi_pkg.vhd:L1144-L1163](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1144-L1163) —— single 版本固定 `rlast='1'`；`expect_rresp_single` 用 `IgnoreData`/`IgnoreResponse` 决定是否跳过两项比对（L1155–L1160），但 `rlast` 必校验（L1161）。

#### 4.4.4 代码实践

**实践目标**：理解 `Response` 参数在不同取值下触发的不同校验路径。

**操作步骤**：

1. 精读 [axi_expect_rresp_burst L1183-L1201](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1183-L1201)。
2. 回答：若调用 `axi_expect_rresp_burst(Beats=>4, Response=>xRESP_SLVERR_c, ...)`，过程会对第 1、2、3 拍校验 `rresp` 吗？对第 4 拍呢？
3. 回答：若把 `Response` 换成 `xRESP_OKAY_c`，4 拍里有几拍会被校验 `rresp`？

**预期结果**：第 2 步——第 1/2/3 拍**不**校验 `rresp`（只校验 `rlast=0` 与数据），第 4 拍校验 `rresp = SLVERR`。第 3 步——4 拍全部校验 `rresp = OKAY`。

#### 4.4.5 小练习与答案

**练习 1**：你希望 master 收完 4 拍读数据但完全不校验数据内容（只校验 `rlast` 和 OKAY 响应），怎么调 `axi_expect_rresp_burst`？

**参考答案**：传 `IgnoreData => true`，`Response => xRESP_OKAY_c`（默认即 OKAY，`IgnoreResponse` 保持 false）。这样每拍 `rlast` 和 OKAY 仍被校验，`rdata` 的比对被跳过。

**练习 2**：`apply_rresp_burst` 能不能在一次突发里让每拍返回不同的 `rresp`？

**参考答案**：不能。`sm.rresp <= Response` 在循环之外赋值（[L1073](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1073)），整个突发的响应码是同一个常量。若需要逐拍不同的响应，只能自己写 process 手动驱动 `sm.rresp`，或拆成多个 single 调用。

---

### 4.5 写响应通道：axi_apply_bresp 与 axi_expect_bresp

#### 4.5.1 概念说明

B 通道是写事务的「收据」：slave 在收完所有 W 拍后回一个 `bresp`（一次写事务**只有一个** BRESP，哪怕是突发）。因此 B 通道没有 burst 变体——`apply_bresp` / `expect_bresp` 都是单拍。

按 4.1 的分工表，B 通道的生产者是 slave，所以这次 `apply_bresp` 驱动 `sm`（`out`），`expect_bresp` 由 master 调用、驱动 `ms`（`out` 的只是 `ms.bready`）。

#### 4.5.2 核心流程

```
apply_bresp(Response, ms, sm, aclk):          -- slave 侧
    sm.bvalid <= '1'
    sm.bresp  <= Response
    wait until rising_edge(aclk) and ms.bready = '1'
    axi_slave_init(sm)                          -- 撤销 bvalid

expect_bresp(Response, ms, sm, aclk):          -- master 侧
    ms.bready <= '1'
    wait until rising_edge(aclk) and sm.bvalid = '1'
    StdlvCompareStdlv(Response, sm.bresp, "wrong BRESP")
    ms.bready <= '0'
```

#### 4.5.3 源码精读

[axi_apply_bresp 实现: hdl/psi_tb_axi_pkg.vhd:L1022-L1031](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1022-L1031) —— slave 抬 `bvalid`、摆 `bresp`，等 master 的 `bready`，握手后调 [axi_slave_init(sm)](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L502-L517) 回收。

[axi_expect_bresp 实现: hdl/psi_tb_axi_pkg.vhd:L1033-L1042](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1033-L1042) —— master 抬 `bready`，等 `bvalid`，用 `StdlvCompareStdlv` 比对响应码，收回 `bready`。

#### 4.5.4 代码实践

**实践目标**：把 `apply_bresp` 与 `expect_bresp` 配成一对完整的 B 通道握手。

**操作步骤**：

1. 在你设想的 master 进程里，写完 W 通道后调用 `axi_expect_bresp(xRESP_OKAY_c, ms, sm, clk)`。
2. 在 slave 进程对应位置调用 `axi_apply_bresp(xRESP_OKAY_c, ms, sm, clk)`。
3. 把 slave 的 `Response` 改成 `xRESP_SLVERR_c`（master 仍期望 OKAY），预测 transcript 输出。

**预期结果**：第 3 步 master 端的 `StdlvCompareStdlv` 会发现 `bresp` 不是 OKAY，打印形如 `###ERROR###: wrong BRESP ...` 的消息（待本地验证确切文案，可对照 [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd) 里 `StdlvCompareStdlv` 的拼接格式）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 B 通道只有 `apply_bresp` / `expect_bresp`，而没有 `*_burst` 变体？

**参考答案**：AXI 协议规定一次写事务（哪怕是多拍突发写）只产生**一个**写响应 BRESP，B 通道天然是单拍，所以不需要 burst 变体。

**练习 2**：一次完整的 AXI 突发写事务，master 侧应当依次调用哪几个过程？slave 侧呢？

**参考答案**：master 侧：`axi_apply_aw` → `axi_apply_wd_burst` → `axi_expect_bresp`。slave 侧（与之并发对拍）：`axi_expect_aw` → `axi_expect_wd_burst` → `axi_apply_bresp`。

---

## 5. 综合实践

**任务**：用 apply/expect 系列拼出一次长度为 4 的 INCR 突发读，并在 master 侧加入 1 拍 `RdyLowCycles` 背压以测试节流。由于仓库没有现成 AXI testbench，你需要自己写一个最小 TB，并在本机仿真器上跑（待本地验证）。

**要点回顾**：

- 4 拍突发 → 地址通道 `AxLen => 3`（= 拍数 − 1），数据通道 `Beats => 4`。
- 32 位数据总线 → `AxSize => AxSIZE_4_c`；`AxBurst => xBURST_INCR_c`。
- 读事务两侧分工：master 发 AR + 收 R；slave 收 AR + 发 R。
- 节流放在 master 的 `expect_rresp_burst`（`RdyLowCycles => 1`），测试 slave（apply 侧）能否正确顶住 `rvalid` 等待 `rready`。

**最小 TB 框架（示例代码，非项目原有代码）**：

```vhdl
-- 示例代码：仅演示 apply/expect 的对拍用法，不是仓库自带文件
library ieee;
use ieee.std_logic_1164.all;
library work;
use work.psi_tb_axi_pkg.all;

entity axi_burst_read_tb is
end entity;

architecture sim of axi_burst_read_tb is
  signal aclk : std_logic := '0';
  signal ms   : axi_ms_r;
  signal sm   : axi_sm_r;
begin
  -- 100 MHz 时钟
  aclk <= not aclk after 5 ns;

  -- master 进程：发 AR，然后收 R（加 1 拍背压）
  p_master : process is
  begin
    axi_master_init(ms);
    wait until rising_edge(aclk);
    axi_apply_ar(AxAddr  => 0,
                 AxSize  => AxSIZE_4_c,
                 AxLen   => 3,                  -- 4 拍 -> AxLEN=3
                 AxBurst => xBURST_INCR_c,
                 ms      => ms, sm => sm, aclk  => aclk);
    axi_expect_rresp_burst(Beats        => 4,
                           DataStart    => 0,
                           DataIncr     => 1,
                           Response     => xRESP_OKAY_c,
                           ms           => ms, sm => sm, aclk => aclk,
                           RdyLowCycles => 1);  -- master 每 2 拍之间背压 1 拍
    wait until rising_edge(aclk);
    report "burst read done" severity note;
    wait;
  end process;

  -- slave 进程：收 AR，然后发 R
  p_slave : process is
  begin
    axi_slave_init(sm);
    wait until rising_edge(aclk);
    axi_expect_ar(AxAddr  => 0,
                  AxSize  => AxSIZE_4_c,
                  AxLen   => 3,
                  AxBurst => xBURST_INCR_c,
                  ms      => ms, sm => sm, aclk => aclk);
    axi_apply_rresp_burst(Beats        => 4,
                          DataStart    => 0,
                          DataIncr     => 1,
                          Response     => xRESP_OKAY_c,
                          ms           => ms, sm => sm, aclk => aclk);
    wait until rising_edge(aclk);
    wait;
  end process;
end architecture;
```

**操作步骤**：

1. 把上面的 TB 存成 `testbench/axi_burst_read_tb.vhd`（仅用于本地学习，不提交、不进 CI）。
2. 在本机 ModelSim 或 GHDL 里编译 `psi_common_math_pkg`、`psi_tb_compare_pkg`、`psi_tb_txt_util`、`psi_tb_axi_pkg` 以及这个 TB（编译顺序参考 [u1-l3](u1-l3-simulation-and-ci.md) 介绍的 PsiSim 流程）。
3. 运行仿真，观察 `ms.arvalid/araddr`、`sm.arready` 的 AR 握手，以及 `sm.rvalid/rdata/rlast` 与 `ms.rready` 的 R 握手。
4. 在波形上确认：因为 `RdyLowCycles => 1`，相邻两拍 R 之间会出现 `ms.rready='0'` 的背压周期，`sm.rvalid` 应保持为 1 直到 master 重新抬 ready。

**需要观察的现象**：

- AR 通道在 1 个上升沿完成握手（`arvalid & arready` 同时为高）。
- R 通道 4 拍数据依次为 `0,1,2,3`，末拍 `rlast=1`。
- 由于背压，4 拍 R 实际占用 > 4 个时钟周期。
- Transcript 中**不应**出现任何 `###ERROR###`（因为 master 与 slave 的期望完全一致）。

**进阶变化**：把 `p_master` 里 `axi_expect_rresp_burst` 的 `DataStart` 改成 `10`（与 slave 发出的 `0` 不匹配），重新跑，确认此时 transcript 出现 `###ERROR###: wrong RDATA ...`——这就把本讲的 apply/expect 对拍与 [u3](u3-l1-compare-basic.md) 的比较检查串了起来。待本地验证。

## 6. 本讲小结

- `axi_apply_*`（驱动方，握手后调 `init` 回收）与 `axi_expect_*`（校验方，抬 ready、收数据、`StdlvCompare*` 比对）是按 AXI **通道**拆分的原子积木；每个通道有一对互补过程，方向由「谁是该通道的生产者」决定。
- 一次完整事务必须由**两个并发进程**对拍完成（master 侧 + slave 侧各调一组过程），这与 `axi_single_*` 的单进程模型根本不同。
- 地址通道的 `AxLen` 是 AXI 协议字段（= 拍数 − 1），与数据通道 `*_burst` 的 `Beats`（拍数本身）是两套数，拼装突发时必须自己换算。
- `VldLowCycles`（apply/生产者侧，拉低 valid）与 `RdyLowCycles`（expect/消费者侧，拉低 ready）只出现在携带数据的突发通道，用来制造非理想吞吐、测试握手状态机鲁棒性。
- 读响应 `expect_rresp_burst` 按 AXI 约定建模：期望 OKAY 时逐拍校验，期望错误时只在末拍校验错误码。
- B 通道单拍、无 burst 变体；`apply_bresp` 驱动 slave 侧、`expect_bresp` 由 master 调用。

## 7. 下一步学习建议

- 若你想把这套 TB 侧 BFM 与一个真正的综合 AXI DUT 接起来，下一步学 [u5-l4：TB 与综合 AXI 类型互转](u5-l4-axi-conversion.md)，它讲解 `psi_tb_axi_conv_pkg` 如何在 `axi_ms_r/axi_sm_r` 与 psi_common 的综合侧类型之间逐字段映射。
- 若你想看一个已经把「两个并发进程对拍」模式落到极致的真实 testbench，可以跳到 [u7-l4：I2C 测试平台实战](u7-l4-i2c-testbench-walkthrough.md)，对照 I2C 的 master/slave 双进程组织方式。
- 想深入理解本讲所有 `expect_*` 依赖的比较底座，回到 [u3-l1](u3-l1-compare-basic.md) 与 [u3-l2](u3-l2-compare-signed-unsigned.md) 精读 `psi_tb_compare_pkg`。
