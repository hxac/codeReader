# 编写并运行一个测试台

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 axi 库里一个**真实测试台**（testbench，以下简称 TB）的完整骨架，理解 `CyclTime` / `ApplTime` / `TestTime` 三个时序参数的含义。
- 掌握 `AXI_LITE` / `AXI_LITE_DV` 双接口 + `AXI_LITE_ASSIGN` 宏这套「标准接线」三明治，并知道为什么要用两层接口。
- 学会用参数化 TB（带 `parameter`）+ 命令行 `-g` 覆盖，来控制事务数量与被测模块的配置。
- 独立为 `axi_lite_join` 写一个最小可跑通的测试台，并报告仿真是否无错。

本讲以 `test/tb_axi_lite_regs.sv` 为走读对象，把前面 u3-l1（驱动器）、u3-l2（随机主从/scoreboard）讲过的零件，拼装成一台完整的「定向随机验证」（directed random verification）机器。

## 2. 前置知识

本讲假设你已经掌握：

- **AXI4-Lite 协议**：比完整 AXI4 简化，没有 `burst` / `id` / `atop`，只有 AW/W/B/AR/R 五个通道，每拍单字节使能 `wstrb`（见 u1-l3）。
- **SystemVerilog `interface` 与 `modport`**：`AXI_LITE.Slave` / `AXI_LITE.Master` 预设了信号方向（见 u2-l3）。
- **typedef/assign 宏体系**：`AXI_LITE_ASSIGN` 在两个接口之间搬信号（见 u2-l4）。
- **驱动器的 TA/TT 时序**：TA（application time，施加激励）与 TT（test time，采样），约定 \(0 \le \text{TA} < \text{TT} < T_{\text{clk}}\)（见 u3-l1）。
- **随机主从**：`axi_lite_rand_master` 自动发包、`axi_lite_rand_slave` 自动回包（见 u3-l2）。

几个本讲会反复出现的术语：

| 术语 | 含义 |
| --- | --- |
| DUT | Device Under Test，被测模块，本讲里是 `axi_lite_regs` |
| TB | Testbench，测试台，文件名约定 `tb_<dut>.sv` |
| directed random | 定向随机：参数控制总量，细节由随机种子决定 |
| `end_of_sim` | 一个普通 `logic` 信号，主进程跑完发包后置 1，通知停止进程收尾 |
| 黄金模型（golden model） | TB 自己用软件算出的「期望值」，用来和 DUT 输出逐拍比对 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [test/tb_axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv) | **本讲主角**：完整的定向随机 TB，是全库 TB 的范本 |
| [src/axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv) | 被测模块 DUT（含接口外壳 `axi_lite_regs_intf`） |
| [src/axi_test.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv) | 提供 `axi_lite_rand_master` / `axi_lite_rand_slave` 类 |
| [src/axi_lite_join.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_join.sv) | 综合实践的 DUT：一个纯透传连接器 |
| [scripts/run_vsim.sh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh) | 仿真脚本，自动发现 `test/tb_*.sv` 并按名字分发 |
| [Makefile](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile) | 把「编译 / 仿真 / 综合」包装成日志目标 |

---

## 4. 核心概念与源码讲解

### 4.1 测试台骨架与时序三参数

#### 4.1.1 概念说明

一个完整的验证 TB 通常由四类「并发块」组成，每类用 `initial` 或 `always` 实现：

1. **时钟与复位发生器**：产生 `clk` 与 `rst_n`。
2. **激励发生器**：例化随机主/从，往 DUT 灌入事务。
3. **自检器（checker）**：旁路监听通道，用黄金模型逐拍比对。
4. **停止控制**：等所有事务发完，留几拍排空，再 `$stop` 结束仿真。

`tb_axi_lite_regs` 顶部一句话点明了它的方法学——**Directed Random Verification**：

[test/tb_axi_lite_regs.sv:15](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L15) 注释 `Directed Random Verification Testbench` 说明它既有「定向」（参数规定写/读各发多少笔）又有「随机」（地址、数据、strobe、等待周期都随机）。

#### 4.1.2 核心流程

整台 TB 的生命周期可以画成一条时间轴：

```text
上电
  │  clk_rst_gen 产生 clk，拉低 rst_n 5 拍后释放
  ▼
rst_n 上升沿
  │  各进程 @(posedge rst_n) 解除阻塞
  │  master.reset() / slave 端清零
  ▼
warm-up：repeat(5) @(posedge clk)
  │
  ├──► 定向测试：write(0x0, ...) / read(0x0, ...) 逐笔断言
  │
  ▼
master.run(TbNoReads, TbNoWrites)   随机发 N 笔
  │  与此同时 4 个 checker 进程不停监听比对
  ▼
end_of_sim <= 1
  │  停止进程：repeat(1000) 排空 + $stop
  ▼
仿真结束
```

时序的灵魂是三个 `localparam time` 常量，它们决定了「在时钟沿之后的哪个瞬间施加激励、哪个瞬间采样」。

#### 4.1.3 源码精读

模块声明带了一堆 `parameter`，这些就是「参数化 TB」的入口（4.5 节详述）：

[test/tb_axi_lite_regs.sv:20-33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L20-L33) 定义模块 `tb_axi_lite_regs` 及其参数：`TbRegNumBytes`（寄存器字节数）、`TbAxiReadOnly`（哪些字节只读）、`TbPrivProtOnly`/`TbSecuProtOnly`（保护位）、`TbNoWrites`/`TbNoReads`（随机事务量）。

三个时序常量是全 TB 共享的「节拍器」：

[test/tb_axi_lite_regs.sv:39-41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L39-L41) 定义 `CyclTime = 10ns`、`ApplTime = 2ns`、`TestTime = 8ns`。其中 `CyclTime` 是时钟周期；`ApplTime` 会被传给驱动器的 TA（施加激励），`TestTime` 会被传给 TT（采样）。

它们在一个时钟周期内的关系是：

\[ 0 \;<\; \text{ApplTime}(2\text{ns}) \;<\; \text{TestTime}(8\text{ns}) \;<\; T_{\text{clk}}(10\text{ns}) \]

```text
clk:  ___|‾‾‾|___|‾‾‾|___      (周期 10ns)
            ^        ^
            |        |
     +2ns 施加激励(ApplTime/TA)   +8ns 采样(TestTime/TT)
     用 <= #TA 驱动 valid/data    用 #TT 后读 ready/resp
```

- **先施加（2ns）后采样（8ns）**：保证同一拍里激励先生效，checker 在接近拍末采样到稳定值。
- **采样留 2ns 余量到下个沿**：模拟真实触发器的 setup time，避免采到翻转中的毛刺。

这两个值随后喂给随机主端（注意 `TA(ApplTime)`、`TT(TestTime)`）：

[test/tb_axi_lite_regs.sv:54-65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L54-L65) 用 `typedef` 给 `axi_lite_rand_master` 起别名 `rand_lite_master_t`，把地址/数据宽度、TA/TT、地址范围、最大并发都钉死，后续 `new` 时就不用再写一遍。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解三个时序常量如何渗透到驱动器。
2. **步骤**：在 [test/tb_axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv) 中找到 `ApplTime`、`TestTime` 的定义（L39-41），再追到 `rand_lite_master_t` 的 `TA`/`TT`（L59-60）。然后打开 [src/axi_test.sv:1554-1573](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1554-L1573) 看 `axi_lite_rand_master` 的参数默认值 `TA = 2ns, TT = 8ns`——正好和 TB 一致。
3. **观察**：注意 TB 没有直接写 `2ns/8ns` 给驱动器，而是先存进 `ApplTime/TestTime` 再转发，这样改一处即全 TB 生效。
4. **预期结果**：你能说清「为什么必须 TA < TT < 周期」。

> 本实践为阅读型，不运行命令；若想动手改值，参考 4.5 节。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TestTime` 改成 `12ns`（大于周期），会出什么问题？
**答案**：采样时刻跨到了下一个时钟周期，checker 会采到下一拍的信号，比对全部错位；且违反 \( \text{TT} < T_{\text{clk}} \) 的约定。

**练习 2**：`CyclTime` 这个常量在本 TB 里被谁消费了？
**答案**：被时钟发生器 `clk_rst_gen` 消费，见 [tb_axi_lite_regs.sv:334-340](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L334-L340) 的 `.ClkPeriod(CyclTime)`。

---

### 4.2 接口声明与 AXI_LITE_ASSIGN 标准接线

#### 4.2.1 概念说明

axi 库的 TB 几乎都用一套固定的「**双接口三明治**」：

```text
   驱动器(driver/类)         可综合 DUT
        │                        │
        │  操作 AXI_LITE_DV       │  端口是 AXI_LITE
        ▼                        ▼
     ┌──────────┐  AXI_LITE_ASSIGN  ┌──────────┐
     │ *_dv     │ ───────────────► │  无 DV   │ ──► DUT
     │ (带 clk) │                   │  接口    │
     └──────────┘                   └──────────┘
```

为什么要两个接口？

- **`AXI_LITE_DV`**：带 `clk_i` 端口，驱动器类和 `assert property` 断言都需要时钟才能工作，所以驱动器绑在 DV 接口上。
- **`AXI_LITE`**：不带时钟，是 DUT 端口使用的「可综合」接口。
- **`AXI_LITE_ASSIGN`**：一行宏把两个接口的同名信号逐根连起来，省去手写几十行 `assign`。

#### 4.2.2 核心流程

接线三步走：

1. 声明无时钟接口 `master`（连 DUT）。
2. 声明带时钟接口 `master_dv(clk)`（连驱动器）。
3. `` `AXI_LITE_ASSIGN(master, master_dv) `` 把两者桥接。

#### 4.2.3 源码精读

[test/tb_axi_lite_regs.sv:81-89](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L81-L89) 正是这套三明治：声明 `AXI_LITE master()`（连 DUT 的 `slv` 端口）和 `AXI_LITE_DV master_dv(clk)`（驱动器操作），最后用 `` `AXI_LITE_ASSIGN(master, master_dv) `` 桥接。注意顶部 [tb_axi_lite_regs.sv:17](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L17) 必须先 `` `include "axi/assign.svh" `` 才能用这个宏。

DUT 侧 `axi_lite_regs_intf` 的端口就是 `AXI_LITE.Slave`：

[src/axi_lite_regs.sv:425-433](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L425-L433) 定义接口外壳 `axi_lite_regs_intf` 的端口，其中 `slv` 是 `AXI_LITE.Slave`，其余是寄存器侧的 `reg_d_i`/`reg_load_i`/`reg_q_o`/`wr_active_o`/`rd_active_o`。

这个外壳内部又用宏把 `AXI_LITE` 接口转成结构体，再喂给真正的内核模块：

[src/axi_lite_regs.sv:438-450](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L438-L450) 先用 `AXI_LITE_TYPEDEF_*` 宏声明五个通道结构体并打包成 `req_lite_t`/`resp_lite_t`，再用 `AXI_LITE_ASSIGN_TO_REQ` / `AXI_LITE_ASSIGN_FROM_RESP` 在接口与结构体之间搬数据。这正是 u2-l4 讲过的「接口外壳 + 结构体内核」标准结构。

回到 TB，`master` 接口最终连到 DUT 的 `slv` 端口：

[test/tb_axi_lite_regs.sv:345-362](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L345-L362) 例化 DUT `axi_lite_regs_intf`，把 `master` 接口接到 `.slv(master)`，寄存器侧端口接到 TB 里的 `reg_d`/`reg_load`/`reg_q` 等信号。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：验证「双接口三明治」的信号流向。
2. **步骤**：在 [test/tb_axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv) 里数一下：声明了几个 `AXI_LITE`？几个 `AXI_LITE_DV`？几个 `AXI_LITE_ASSIGN`？
3. **观察**：本 TB 只有 1 个 master 侧（DUT 只有一个 slave 端口）。如果你要测 `axi_lite_join`（一进一出），就需要 **两套** 三明治（master 侧 + slave 侧），这正是综合实践的关键。
4. **预期结果**：你能画出 join TB 需要的两套接口图。

#### 4.2.5 小练习与答案

**练习 1**：为什么驱动器类必须绑 `AXI_LITE_DV` 而不能直接绑 `AXI_LITE`？
**答案**：驱动器内部要用 `@(posedge axi.clk_i)` 做时序控制，而 `AXI_LITE` 没有 `clk_i`，只有 `AXI_LITE_DV` 才带时钟端口。

**练习 2**：`` `AXI_LITE_ASSIGN `` 宏展开后大致是什么？
**答案**：一组 `assign master.aw_addr = master_dv.aw_addr;` …… 之类的逐信号连接，把 AW/W/B/AR/R 五个通道的所有信号在两个接口间桥接（详见 u2-l4）。

---

### 4.3 激励发生：rand_lite_master 的例化与 run()

#### 4.3.1 概念说明

激励由 `axi_lite_rand_master` 产生。它内部包装了 u3-l1 的底层 `axi_lite_driver`，把「发一笔合法事务」封装成 `write()` / `read()` / `run()`。本 TB 的发包分两段：

- **定向段**：先发一笔地址固定为 `0x0` 的写、再读同一地址，用 `assert` 立刻检查响应码——验证「已知寄存器」的基本行为。
- **随机段**：调用 `run(TbNoReads, TbNoWrites)`，内部 `fork` 五个并发任务，随机发 N 笔读 + M 笔写。

#### 4.3.2 核心流程

`run()` 的内部结构（[src/axi_test.sv:1686-1695](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1686-L1695)）是「五路并发、`join` 汇合」：

```text
run(n_reads, n_writes):
  fork
    send_ars(n_reads)   // 发 n_reads 个 AR（地址随机）
    recv_rs(n_reads)    // 收 n_reads 个 R
    send_aws(n_writes)  // 发 n_writes 个 AW
    send_ws(n_writes)   // 发 n_writes 个 W（数据/strobe 随机）
    recv_bs(n_writes)   // 收 n_writes 个 B
  join                  // 五路都完成才返回
```

读写各走各的、AW 与 W 解耦发送、用内部队列 `aw_queue`/`b_queue`/`w_queue` 维持配对关系——这套并发模型让激励更接近真实主端的行为。

#### 4.3.3 源码精读

主进程 `proc_generate_axi_traffic` 是 TB 的「指挥」：

[test/tb_axi_lite_regs.sv:95-131](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L95-L131) 关键节奏：
- L96 `new(master_dv, "Lite Master")` 把驱动器绑到 DV 接口；
- L100 `lite_axi_master.reset()` 复位主端（按 modport 清零本侧输出）；
- L101-102 `@(posedge rst_n)` 后再 `repeat(5)` 预热；
- L105-106 定向写一笔 `0x0`，数据 `0xDEADBEEF...`、strobe `0xFF`；
- L111-112 `assert (resp == RESP_OKAY)` 立即断言响应；
- L129 `lite_axi_master.run(TbNoReads, TbNoWrites)` 进入随机段；
- L130 `end_of_sim <= 1'b1` 通知停止进程。

`write()` / `read()` 这两个定向任务的实现，确认它们就是「并发发 AW+W，再收 B」/「发 AR，再收 R」：

[src/axi_test.sv:1697-1720](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1697-L1720) `write` 用 `fork...join` 并发 `send_aw` 与 `send_w`，再 `recv_b`；`read` 先 `send_ar` 再 `recv_r`。两者都通过底层驱动器施加激励（受 TA 约束）与采样（受 TT 约束）。

注意本 TB **没有** 例化 `axi_lite_rand_slave`——因为 DUT 本身就是个 AXI-Lite 从端（寄存器），主端直接和 DUT 握手即可。综合实践里测 `axi_lite_join`（纯透传、自己不回响应）时，才需要在另一端挂一个 `axi_lite_rand_slave` 来扮演「真从端」。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理清随机段事务量的来源。
2. **步骤**：在 [tb_axi_lite_regs.sv:129](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L129) 看到 `run(TbNoReads, TbNoWrites)`；往上找到 `TbNoReads`/`TbNoWrites` 的定义（L30-32，默认 1500/1000）。
3. **观察**：这两个值是模块 `parameter`，意味着可在命令行用 `-gTbNoReads=...` 覆盖（见 4.5）。
4. **预期结果**：你能解释「为什么仿真跑很久」——默认要发 2500 笔随机事务。

#### 4.3.5 小练习与答案

**练习 1**：`run()` 用的是 `fork...join` 而不是 `join_none`，这意味着什么？
**答案**：`join` 会阻塞到五路任务**全部完成**才返回，所以 `run()` 返回时所有 N 笔读、M 笔写都已发完并收到响应——主进程才能放心地置 `end_of_sim`。

**练习 2**：定向写 `0x0` 时为什么 `resp` 期望是 `RESP_OKAY`（当 `PrivProtOnly`/`SecuProtOnly` 都为 0 时）？
**答案**：因为字节 0 在默认配置下不是只读位、地址合法、保护位不受限，DUT 应正常写入并回 `OKAY`（见 [tb_axi_lite_regs.sv:110-113](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L110-L113)）。

---

### 4.4 自检闭环：checker 进程、end_of_sim 与停止

#### 4.4.1 概念说明

光发激励不算验证，必须**自检**。本 TB 用了三种自检手段叠加：

1. **定向 `assert`**：在主进程里对已知事务的响应码立即断言（4.3 已见）。
2. **并发 checker 进程**：用独立的 `initial` 逐拍监听通道，用软件黄金模型算期望值并比对——这是随机段的主力。
3. **`assert property`**：用并发断言检查协议不变量（如只读位不能被 AXI 写改）。

而「什么时候停仿真」由一个普通信号 `end_of_sim` 协调：主进程发完包置 1，停止进程检测到后排空若干拍再 `$stop`。

#### 4.4.2 核心流程

读通道自检的黄金模型思路：

```text
每拍 @(posedge clk); #TestTime:
  if (AR 通道握手) :
      按 ar_addr 算出这一拍读到的应该是哪些字节
      查 reg_q[]（DUT 当前寄存器值）拼出期望 rdata / resp
      push 进期望队列
  if (R 通道握手) :
      pop 期望值
      逐字节比对 master.r_data（用 !== 容忍 8'hxx 不确定位）
      不符则 $error
```

#### 4.4.3 源码精读

读数据 checker 用两个队列 `exp_rdata[$]`/`exp_resp[$]` 做「先存期望、到时再比」：

[test/tb_axi_lite_regs.sv:161-214](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L161-L214) `proc_check_read_data`：AR 握手时按地址算期望数据并 `push_back`（L171-193），R 握手时 `pop_front` 并逐字节比对（L196-212）。注意 L201 用 `exp_byte !== 8'hxx` 跳过未初始化字节，L208 校验错误响应返回固定 `0xBA5E1E55`（与 DUT 的默认错误数据一致，见 [axi_lite_regs.sv:288](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L288)）。

写数据 / B 响应 checker 同理，还顺带检查 `wr_active` 与只读位：

[test/tb_axi_lite_regs.sv:219-285](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L219-L285) `proc_check_write_data` 在 AW&W 同时握手时算期望 B 响应（含保护位/只读位判定），并对每个被 strobe 的字节 fork 出 `check_q` 验证写入了正确值；`proc_check_b` 用 `b_resp_queue` 比对实际 B 响应。

并发断言保护协议不变量：

[test/tb_axi_lite_regs.sv:303-322](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L303-L322) 用 `genvar` 为每个字节生成 `assert property`：只读位在无直接装载时必须 `$stable`、直接装载时必须等于上一拍 `reg_d`、非只读位不能同时被 AXI 写与直接装载等。`default disable iff (~rst_n)` 保证复位期间不报错。

停止进程用 `end_of_sim` 做软停：

[test/tb_axi_lite_regs.sv:324-329](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L324-L329) `proc_stop_sim`：`wait(end_of_sim)` 后再 `repeat(1000)` 排空在途事务，然后 `$display` + `$stop`。这里故意排空 1000 拍，确保 checker 把最后几笔响应也比完。

时钟与复位来自外部依赖 `common_cells` 的 `clk_rst_gen`（本仓库 `src/` 里没有这个模块，它由 Bender 的 `common_cells` 依赖提供）：

[test/tb_axi_lite_regs.sv:334-340](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L334-L340) 例化 `clk_rst_gen`，`.ClkPeriod(CyclTime)`、`.RstClkCycles(5)`，输出 `clk` 与低有效 `rst_n`。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：体会「黄金模型」如何容忍不确定值。
2. **步骤**：读 [tb_axi_lite_regs.sv:176-183](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L176-L183)：当某字节落在 `RegNumBytes` 之外时，期望数据填 `8'hxx`。
3. **观察**：再看 L201 的 `if (exp_byte !== 8'hxx)`——比对时主动跳过 `x` 字节。
4. **预期结果**：理解这种「用 x 通配合法的不确定性」是随机验证避免误报的关键技巧。

#### 4.4.5 小练习与答案

**练习 1**：为什么 checker 用 `!==` 而不是 `!=` 来比较 `8'hxx`？
**答案**：`!=` 对含 `x` 的比较结果也是 `x`（假），无法可靠区分；`!==` 是按位比较且对 `x`/`z` 敏感，`8'hxx !== 8'hxx` 为假，从而能正确「识别并跳过」不确定字节。

**练习 2**：`end_of_sim` 为什么要排空 1000 拍才 `$stop`？
**答案**：主进程置 `end_of_sim` 时，可能还有在途事务（已发 AR/AW 但响应未回），checker 需要时间收完最后的 R/B 并比对；排空保证不漏检。

---

### 4.5 用参数化 TB 控制事务数量与配置

#### 4.5.1 概念说明

把 `parameter` 放在 TB 模块顶部，就能在**不重新编译源码**的前提下，从命令行用 `-g<ParamName>=<Value>` 覆盖默认值。这是回归测试（regression）的基石：同一份 TB，跑多种配置组合。

#### 4.5.2 核心流程

```text
TB 顶部声明 parameter TbXxx = 默认值
        │
        ▼
命令行：vsim -gTbXxx=新值 tb_xxx
        │
        ▼
脚本 scripts/run_vsim.sh 按名字分发，拼出合适的 -g 组合
```

#### 4.5.3 源码精读

TB 的参数既是事务量、也是 DUT 配置：

[test/tb_axi_lite_regs.sv:20-33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L20-L33) `TbRegNumBytes`（DUT 字节数）、`TbAxiReadOnly`（DUT 只读位图）、`TbPrivProtOnly`/`TbSecuProtOnly`（DUT 保护位）、`TbNoWrites`/`TbNoReads`（事务量）全是 `parameter`。

这些参数随后透传给 DUT 例化：

[test/tb_axi_lite_regs.sv:345-353](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L345-L353) DUT 的 `.REG_NUM_BYTES(TbRegNumBytes)`、`.PRIV_PROT_ONLY(TbPrivProtOnly)` 等，全部由 TB 参数驱动。

`run_vsim.sh` 为 `axi_lite_regs` 拼出 4×3×种子的配置矩阵：

[scripts/run_vsim.sh:147-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L147-L157) 对 `axi_lite_regs` 用三重循环：`PRIV∈{0,1}` × `SECU∈{0,1}` × `BYTES∈{42,200,369}`，每次 `call_vsim tb_axi_lite_regs -gTbPrivProtOnly=$PRIV -gTbSecuProtOnly=$SECU -gTbRegNumBytes=$BYTES`，并额外加种子 `10`、`42`。一个 TB 就这样跑出几十种配置。

底层 `call_vsim` 用种子 `sv_seed` 跑、并用日志内容判成败：

[scripts/run_vsim.sh:30-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L30-L35) 对每个 `seed` 执行 `vsim -sv_seed $seed`，仿真完 `grep "Errors: 0," vsim.log`——**以日志里这一行是否存在来判定通过**（而不是返回码）。

#### 4.5.4 代码实践（命令行实践）

1. **目标**：用 `-g` 覆盖 TB 参数跑一次轻量仿真。
2. **步骤**：先 `make compile.log` 编译；然后手动跑一个最小配置（待本地验证，需 Questasim/vsim 环境）：

   ```bash
   cd build
   echo "run -all" | vsim -sv_seed 0 -t 1ps \
     -gTbRegNumBytes=42 -gTbNoWrites=50 -gTbNoReads=50 \
     tb_axi_lite_regs | tee lite_regs.log
   grep "Errors: 0," lite_regs.log
   ```

3. **观察**：相比默认（200 字节、2500 笔），事务量小一个数量级，仿真更快。
4. **预期结果**：日志末尾出现 `# Errors: 0, ...` 行即代表无错。**若没有 vsim 环境，标注「待本地验证」并改为阅读 [run_vsim.sh:147-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L147-L157) 理解配置矩阵。**

#### 4.5.5 小练习与答案

**练习 1**：`make sim-axi_lite_regs.log` 实际会跑多少次 vsim？
**答案**：看 [run_vsim.sh:147-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L147-L157) 与 L28 的种子列表：`SEEDS=(0)` 再 `+= (10 42)` 共 3 个种子，乘以 `PRIV(2)×SECU(2)×BYTES(3)=12` 组，共 36 次 vsim（注：Makefile 的 `sim-%.log` 还会因 `--random-seed` 再加一个随机种子）。

**练习 2**：为什么用 `grep "Errors: 0,"` 而不是看 vsim 退出码？
**答案**：vsim 即使遇到 `$error` 也常以退出码 0 结束，退出码不可靠；日志里的 `Errors: 0,` 统计行才是仿真器自己对错误计数的权威结论（见 u1-l4）。

---

## 5. 综合实践：为 axi_lite_join 写一个最小测试台

把本讲的「双接口三明治 + 随机主从 + end_of_sim 停止」四件套用起来，亲手造一台 TB。

### 5.1 实践目标

`axi_lite_join_intf` 是一个**纯透传连接器**——一端 `AXI_LITE.Slave in`、一端 `AXI_LITE.Master out`，内部仅一行 `` `AXI_LITE_ASSIGN(out, in) ``：

[src/axi_lite_join.sv:19-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_join.sv#L19-L35) 模块 `axi_lite_join_intf`，核心就是 L24 的 `` `AXI_LITE_ASSIGN(out, in) ``。

它自己不发响应，所以必须在一端挂随机主端、另一端挂随机从端，让事务穿过 join 来回路由。目标：跑通后报告仿真是否无错。

### 5.2 操作步骤

1. **新建文件** `test/tb_axi_lite_join.sv`（注意：这是你在本地创建的练习文件，不属于本仓库原代码）。
2. **填入下面的示例代码**（这是为练习编写的**示例代码**，仿照 `tb_axi_lite_regs` 的骨架裁剪而来）：

   ```systemverilog
   `include "axi/assign.svh"

   // 示例代码：axi_lite_join 的最小定向随机测试台
   module tb_axi_lite_join #(
     parameter int unsigned TbNoWrites = 32'd500,
     parameter int unsigned TbNoReads  = 32'd500
   );
     localparam int unsigned AxiAddrWidth = 32'd32;
     localparam int unsigned AxiDataWidth = 32'd32;
     localparam time CyclTime = 10ns;
     localparam time ApplTime =  2ns;
     localparam time TestTime =  8ns;

     // 随机主/从类型别名（钉死宽度与 TA/TT）
     typedef axi_test::axi_lite_rand_master #(
       .AW ( AxiAddrWidth ), .DW ( AxiDataWidth ),
       .TA ( ApplTime ),     .TT ( TestTime ),
       .MAX_READ_TXNS (10),  .MAX_WRITE_TXNS (10)
     ) rand_lite_master_t;
     typedef axi_test::axi_lite_rand_slave #(
       .AW ( AxiAddrWidth ), .DW ( AxiDataWidth ),
       .TA ( ApplTime ),     .TT ( TestTime )
     ) rand_lite_slave_t;

     logic clk, rst_n, end_of_sim;

     // ---- master 侧三明治：主端驱动 -> join.in ----
     AXI_LITE  #(.AXI_ADDR_WIDTH(AxiAddrWidth), .AXI_DATA_WIDTH(AxiDataWidth)) master ();
     AXI_LITE_DV #(.AXI_ADDR_WIDTH(AxiAddrWidth), .AXI_DATA_WIDTH(AxiDataWidth)) master_dv (clk);
     `AXI_LITE_ASSIGN(master, master_dv)

     // ---- slave 侧三明治：join.out -> 随机从端 ----
     AXI_LITE  #(.AXI_ADDR_WIDTH(AxiAddrWidth), .AXI_DATA_WIDTH(AxiDataWidth)) slave ();
     AXI_LITE_DV #(.AXI_ADDR_WIDTH(AxiAddrWidth), .AXI_DATA_WIDTH(AxiDataWidth)) slave_dv (clk);
     `AXI_LITE_ASSIGN(slave, slave_dv)

     // ---- DUT：纯透传连接器 ----
     axi_lite_join_intf i_join ( .in ( master ), .out ( slave ) );

     // ---- 激励：主端发包，从端自动回包 ----
     initial begin : proc_stim
       automatic rand_lite_master_t mst = new(master_dv, "Master");
       automatic rand_lite_slave_t slv = new(slave_dv,  "Slave");
       end_of_sim <= 1'b0;
       mst.reset();
       slv.reset();
       @(posedge rst_n);
       repeat (5) @(posedge clk);
       fork
         slv.run();          // 从端 run() 无参数、内部 forever，必须用 join_none 分离
       join_none
       mst.run(TbNoReads, TbNoWrites);  // 主端发完才返回
       end_of_sim <= 1'b1;
     end

     // ---- 停止：排空后 $stop ----
     initial begin : proc_stop
       wait (end_of_sim);
       repeat (100) @(posedge clk);
       $display("Simulation stopped as Master transferred its data.");
       $stop();
     end

     // ---- 时钟与复位（common_cells 提供）----
     clk_rst_gen #(.ClkPeriod(CyclTime), .RstClkCycles(5)) i_clk_gen (
       .clk_o(clk), .rst_no(rst_n)
     );
   endmodule
   ```

   关键点对照本讲内容：
   - **两套三明治**（4.2 节）：`master`/`master_dv` 与 `slave`/`slave_dv`，分别用 `AXI_LITE_ASSIGN` 桥接。
   - **时序三参数**（4.1 节）：`CyclTime/ApplTime/TestTime` 透传给主从的 `TA/TT`。
   - **从端必须 detach**：`axi_lite_rand_slave.run()` 是 [src/axi_test.sv:1844-1852](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1844-L1852) 的 `fork...join` 五个 `forever` 任务，永不返回，所以用 `fork...join_none` 分离，再调 `mst.run()`。
   - **`end_of_sim` 软停**（4.4 节）：主端 `run()` 返回后置位，停止进程排空 100 拍再 `$stop`。

3. **编译**：`make compile.log`（会按 Level 0–6 顺序编译全库）。
4. **运行**（二选一，待本地验证，需 vsim 环境）：

   ```bash
   # 方式 A：用 Makefile 的 sim-%.log 目标（推荐）
   make sim-axi_lite_join.log

   # 方式 B：直接调脚本
   cd build && ../scripts/run_vsim.sh axi_lite_join
   ```

   说明：`axi_lite_join` 虽然不在 [Makefile:22-40](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L22-L40) 的 `TBS` 列表里，但 `sim-%.log` 模式规则会把名字透传给 `run_vsim.sh`；脚本在 [run_vsim.sh:37-41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L37-L41) 校验 `test/tb_axi_lite_join.sv` 存在后，落到默认分支 [run_vsim.sh:244-246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L244-L246) 执行 `call_vsim tb_axi_lite_join ...`。

### 5.3 需要观察的现象

- 仿真应打印 `Run for Reads 500, Writes 500`（来自 `axi_lite_rand_master.run` 的 `$display`）。
- 末尾出现 `Simulation stopped as Master transferred its data.`。
- **关键判定行**：日志里应能 `grep` 到 `Errors: 0,`（由 `call_vsim` 自动检查）。

### 5.4 预期结果

`axi_lite_join` 是纯透传，随机主从又只产生合法激励，因此仿真应**无错通过**：`Errors: 0,` 行存在，`make` 目标不会因 `(! grep "Error:")`/`(! grep "Fatal:")` 失败。如果出现握手死锁，最常见原因是忘了用 `join_none` 分离 `slv.run()`——它会让 `proc_stim` 永远卡在 `join` 上，主端发不出包。

> 若本地无 vsim，标注「待本地验证」；可改为阅读型实践：对照上面的示例代码，逐行说明每个块对应本讲 4.1–4.4 的哪个概念。

---

## 6. 本讲小结

- 一个完整 TB 由**时钟复位 / 激励 / 自检 / 停止**四类并发块组成；`tb_axi_lite_regs` 是全库范本。
- 时序三参数 `CyclTime(10ns)/ApplTime(2ns)/TestTime(8ns)` 满足 \(0<\text{TA}<\text{TT}<T_{\text{clk}}\)，分别决定时钟周期、施加激励时刻、采样时刻。
- **双接口三明治**：驱动器操作带时钟的 `AXI_LITE_DV`，DUT 用 `AXI_LITE`，两者用 `` `AXI_LITE_ASSIGN `` 桥接。
- 激励分**定向段**（`write()/read()` + 立即 `assert`）与**随机段**（`run(n_reads,n_writes)` 内部五路并发）。
- 自检靠**并发 checker 进程**（用队列存期望、用 `!==` 容忍 `x`）+ **`assert property`**（协议不变量）双重保障。
- 参数化 TB：模块顶部 `parameter` + 命令行 `-g` 覆盖，`run_vsim.sh` 用循环拼配置矩阵，以日志 `Errors: 0,` 判成败。

## 7. 下一步学习建议

- **横向对比**：去看 `test/tb_axi_lite_mailbox.sv` 或 `test/tb_axi_lite_xbar.sv`，它们有**多个**主/从端口，是「双接口三明治」的多端口扩展，能加深对本讲接线模式的理解。
- **进入进阶层**：本讲是入门层最后一篇。下一单元 U4 起进入**总线连接原语**（`axi_join` / `axi_cut` / `axi_modify_address`），届时你会用本讲学到的 TB 写法去验证完整 AXI4（非 Lite）模块——建议先复习 u2-l4 的 `req_t`/`resp_t` 结构体与 `AXI_ASSIGN` 宏。
- **验证方法学深入**：若对随机验证感兴趣，可跳读 U16 的「定向随机验证方法学」（u16-l1），看 `tb_axi_xbar` 如何把本讲的套路放大到完整 AXI4 交叉开关。
