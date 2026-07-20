# 仿真平台：testbench 与 AXI 仿真过程

## 1. 本讲目标

本讲是进阶单元的第一篇，带读者走进 `vivadoIP_mem_test` 的验证代码 `tb/top_tb.vhd`。读完本讲，你应当能够：

- 说清楚 testbench 里**两个并发进程** `p_control` 与 `p_axi` 各自扮演什么角色，以及它们如何通过 `SetupDone` / `AxiDone` 两个信号完成「配置 → 仿真存储器 → 校验结果」的握手。
- 看懂 psi_tb 库提供的 AXI 辅助过程（`axi_single_write` / `axi_single_expect` / `axi_expect_aw` / `axi_expect_wd_burst` / `axi_apply_rresp_burst` 等）在 testbench 中的用法。
- 理解**错误注入**的核心手法：在 AXI4 读响应里改变每拍数据的递增量（`Incr_v`），从而人为制造错误，验证硬件的 `ERRORS` 计数与 `FIRSTERR` 首个错误地址是否正确。

本讲承接 u3-l3（主状态机）与 u3-l4（pattern 生成与首个错误地址换算）：状态机告诉了我们「硬件在什么阶段做什么」，pattern 章节告诉了我们「每拍期望数据是什么、错误如何计数」，本讲则在仿真侧把这些期望**逐一比对**，并故意投放错误来检验硬件。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

- **testbench（测试平台）**：一段不对应真实硬件、只在仿真器里运行的 VHDL 代码。它实例化被测设计（DUT, Design Under Test），给它喂激励、观察它的输出、并在输出不符合预期时报错。
- **DUT**：本讲的 DUT 就是 `mem_test_wrapper`（见 u3-l1），一个对外有两组 AXI 接口的 IP。
- **AXI 主从方向**：AXI 是点对点协议，一端叫 master（主动发起读写），另一端叫 slave（被动响应）。本项目的 DUT 同时是两种角色：
  - 在控制面 `S00_AXI` 上，DUT 是 **slave**，CPU（这里由 testbench 扮演）是 **master**，通过它配置寄存器、启动测试、读状态。
  - 在数据面 `M00_AXI` 上，DUT 是 **master**，被测存储器（这里也由 testbench 扮演）是 **slave**，DUT 通过它突发写 / 突发读数据。
- **burst（突发传输）**：一次 AXI4 读写命令可以连续搬运多个数据拍（beat），中间地址自动递增，称为 INCR burst。本讲里一次测试就是把一片地址区域拆成若干个 16 拍的 burst 来搬。
- **beat（拍）**：burst 里的单个数据传输。本项目数据宽 32 位 = 4 字节，因此每个 beat 覆盖 4 个字节地址；第 \(k\) 拍（从 1 起算）覆盖的字节地址为

  \[
  \text{Addr}_k = \text{Base} + 4(k-1)
  \]

- **`###ERROR###` 约定**：testbench 用 `assert ... report "###ERROR### ..." severity error` 来宣告失败。CI 脚本 `ciFlow.py` 会扫描 Transcript 里有没有这个字符串（详见 u1-l3）。本讲的 testbench 大量使用这一约定。

> 说明：本讲引用的 `axi_single_write`、`axi_expect_aw`、`axi_apply_rresp_burst` 等过程来自**外部依赖库 `psi_tb`**（`psi_tb_axi_pkg`），它不在本仓库内（参见 u1-l2 关于依赖的说明）。因此下文只依据它们在 `top_tb.vhd` 中的**真实调用方式**来讲解其含义，不臆造库内部的实现细节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd) | 唯一的 testbench。实例化 DUT、产生时钟、用 `p_control` 配置寄存器并用 `p_axi` 仿真存储器，覆盖 7 个测试用例。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl) | 仿真配置脚本，声明把 `top_tb.vhd` 作为 testbench 并创建一次运行（详见 u1-l3）。 |
| hdl/mem_test_pkg.vhd | 寄存器地址常量 `REG_*` 与状态/模式/pattern 枚举（详见 u2-l1），testbench 直接 `use` 它来写寄存器地址。 |
| hdl/mem_test_wrapper.vhd | 被测的顶层 DUT（详见 u3-l1）。 |

## 4. 核心概念与源码讲解

### 4.1 控制进程与寄存器配置

#### 4.1.1 概念说明

`p_control` 进程扮演**CPU**：它通过 `S00_AXI`（AXI-Lite 从机）向 DUT 写寄存器来配置一次内存测试，启动测试，等待测试结束，再读回状态与错误统计并比对。这正是 u2-l3 里描述的 C 驱动要做的事，只不过这里用 VHDL 过程而非 C 函数来完成。

它和 `p_axi` 是**两个并发进程**，必须协调：`p_control` 配置完寄存器、写 START 之后，`p_axi` 才能开始仿真存储器；`p_axi` 仿真完一片区域的读写之后，`p_control` 才能去读结果。协调靠两个共享信号完成。

#### 4.1.2 核心流程

```text
p_control                          p_axi
─────────                          ─────
写 MODE/SIZE/ADDR/PATTERN
写 START
SetupDone <= N          ───────►   wait until SetupDone = N
                                   仿真存储器突发写 + 突发读
wait until AxiDone = N   ◄───────  AxiDone <= N
读 STATUS / ERRORS / FERR 并断言
```

两个同步信号声明为整数、初值 `-1`，因此「等待等于某个用例编号 N」从 N=0 开始都能正常触发。

#### 4.1.3 源码精读

同步信号声明，初值 `-1`：[tb/top_tb.vhd:L79-L80](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L79-L80)

```vhdl
signal SetupDone : integer := -1;
signal AxiDone   : integer := -1;
```

复位序列（拉低 `aresetn`、等一段时间、再拉高）：[tb/top_tb.vhd:L269-L275](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L269-L275)

以**用例 0（OwnAddress pattern，成功）**为例，`p_control` 的配置序列是：写 `REG_MODE`、`REG_SIZE_LO`、`REG_PATTERN_SEL`、`REG_ADDR_LO`、`REG_START`，然后 `SetupDone <= 0`：[tb/top_tb.vhd:L279-L285](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L279-L285)

```vhdl
axi_single_write(REG_MODE*4,        C_MODE_SINGLE,        s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_SIZE_LO*4,     16#100#,              s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_PATTERN_SEL*4, C_PATTERN_SEL_OWNADD, s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_ADDR_LO*4,     16#A8#,               s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_START*4,       1,                    s_axi_ms, s_axi_sm, aclk);
SetupDone <= 0;
wait until AxiDone = 0;
```

几点要读出来的含义：

- `REG_MODE*4`：`REG_MODE` 是寄存器**编号**（=3，见 u2-l1），乘 4 得字节地址 0x0C。`axi_single_write` 的第一参数是字节地址。
- 第二参数是要写的值，直接用 `mem_test_pkg` 里的枚举常量（如 `C_MODE_SINGLE`、`C_PATTERN_SEL_OWNADD`），数值与硬件一致。
- `s_axi_ms` / `s_axi_sm` 是打包了所有 AXI-Lite 信号的记录：`ms`（master 侧）由 testbench 驱动，`sm`（slave 侧）由 DUT 驱动。这与「CPU 是 master、DUT 寄存器接口是 slave」的方向一致。
- `SetupDone <= 0` 唤醒 `p_axi` 开始第 0 个用例的存储器仿真；`wait until AxiDone = 0` 等它做完。

测试结束后用 `axi_single_expect` 读寄存器并断言期望值（不匹配则打印带 `###ERROR###` 的消息）：[tb/top_tb.vhd:L287-L288](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L287-L288)

```vhdl
axi_single_expect(REG_STATUS*4, C_STATUS_IDLE, s_axi_ms, s_axi_sm, aclk, "Status not idle 0");
axi_single_expect(REG_ERRORS*4, 0,             s_axi_ms, s_axi_sm, aclk, "Unexpected Errors 0");
```

用例 1（错误用例）里还演示了**轮询 STATUS**：在测试进行中反复读 `REG_STATUS`，先等到 `C_STATUS_WRITING`，再等到 `C_STATUS_READING`，借机观察主状态机的对外状态映射：[tb/top_tb.vhd:L300-L306](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L300-L306)

最后，所有用例跑完，`p_control` 把 `TbRunning` 置 `false` 让时钟进程退出循环、结束仿真：[tb/top_tb.vhd:L400-L401](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L400-L401)

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 `p_control` 里 7 个用例的「配置 → 同步 → 校验」三段结构整理成一张表。
2. **操作步骤**：阅读 [tb/top_tb.vhd:L265-L402](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L265-L402)，对每个用例（编号 0~6）记录：用了哪个 MODE、哪个 PATTERN、测试地址与大小、`SetupDone` 赋的值、`axi_single_expect` 期望的 `ERRORS` 与 `FERR_ADDR`。
3. **需要观察的现象**：每个用例都严格遵循 `配置 → SetupDone<=N → (可选)轮询 → wait AxiDone=N → expect` 的模板。
4. **预期结果**：你会得到一张 7 行的表，其中用例 0/3/5/6 期望 0 错误，用例 1/2/4 期望非零错误（这正是下一节要讲错误注入的用例）。
5. 本实践无需运行仿真，纯阅读即可；若要运行，见 4.3.4。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `SetupDone` 和 `AxiDone` 初值是 `-1` 而不是 `0`？
  - **答案**：因为用例编号从 0 开始。若初值是 0，`p_axi` 里 `wait until SetupDone = 0` 会在仿真开始、`p_control` 还没来得及配置时就立刻通过，造成时序错乱。初值 `-1` 保证「等待第 0 个用例」也必须等到 `p_control` 真正赋值之后。
- **练习 2**：`axi_single_write(REG_MODE*4, ...)` 里为什么是 `REG_MODE*4`，而 `axi_single_expect(REG_ERRORS*4, ...)` 也是 `*4`？
  - **答案**：`REG_MODE`、`REG_ERRORS` 是寄存器**编号**（index），不是字节地址。AXI-Lite 按字节寻址、每个 32 位寄存器占 4 字节，故字节地址 = 编号 × 4。这与 u2-l1、u4-l1 的结论一致。

### 4.2 AXI4 仿真响应进程

#### 4.2.1 概念说明

`p_axi` 进程扮演**被测存储器**（AXI4 slave）。DUT 在 `M00_AXI` 上是 master，会发起突发写（写 pattern）和突发读（回读比对）。`p_axi` 的职责是：

1. **校验 DUT 发出的命令与写数据是否符合预期**（地址、burst 长度、每拍数据）；
2. **回送读数据**给 DUT，让 DUT 的回读比对逻辑有数据可比。

注意方向：在 `M00_AXI` 上，`m_axi_ms`（master 侧）由 **DUT** 驱动，`m_axi_sm`（slave 侧）由 **testbench** 驱动。这与控制面正好相反，是理解本节的关键。

#### 4.2.2 核心流程

以一次「OwnAddress pattern、16 拍 burst」为例，`p_axi` 对**写阶段**和**读阶段**各跑一个循环：

```text
写阶段（DUT 发 AW+W，p_axi 当存储器接收）：
  for 每个 burst（地址从 Base 起，每次 +16*4 字节）:
      axi_expect_aw(地址, SIZE, 16-1, INCR)        -- 期望 DUT 发来这个写命令
      axi_expect_wd_burst(16, 起始数据, 增量, ...)  -- 接收并校验 16 拍写数据
      axi_apply_bresp(OKAY)                         -- 回写响应

读阶段（DUT 发 AR，p_axi 当存储器回送 R）：
  for 每个 burst:
      axi_expect_ar(地址, SIZE, 16-1, INCR)        -- 期望 DUT 发来这个读命令
      axi_apply_rresp_burst(16, 起始数据, 增量, OKAY) -- 回送 16 拍读数据
```

每个 burst 16 拍、每拍 4 字节，故一个 burst 覆盖 64 字节；地址每轮 `+16*4 = +64`。

#### 4.2.3 源码精读

`M00_AXI` 侧的记录信号与位宽常量：[tb/top_tb.vhd:L54-L70](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L54-L70)。注意 `M_DATA_WIDTH=32`、`M_ADDR_WIDTH=16`，与 DUT 实例化时的 generic 一致：[tb/top_tb.vhd:L167-L170](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L167-L170)

用例 0 的写阶段循环（从 0xA8 到 <0x1A8，每轮 +64 字节，共 4 个 burst）：[tb/top_tb.vhd:L415-L421](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L415-L421)

```vhdl
Addr_v := 16#A8#;
while Addr_v < 16#1A8# loop
    axi_expect_aw   (Addr_v, AxSIZE_4_c, 16-1, xBURST_INCR_c, m_axi_ms, m_axi_sm, aclk);
    axi_expect_wd_burst(16, Addr_v, 4, "1111", "1111", m_axi_ms, m_axi_sm, aclk);
    axi_apply_bresp (xRESP_OKAY_c, m_axi_ms, m_axi_sm, aclk);
    Addr_v := Addr_v + 16*4;
end loop;
```

可读出的含义：

- `axi_expect_aw(地址, AxSIZE_4_c, 16-1, xBURST_INCR_c, ...)`：期望 DUT 发来一个写地址命令，传输大小为 4 字节（`AxSIZE_4_c` 表示 \(2^2=4\) 字节/拍）、长度 16 拍（AXI 的 len 字段是「拍数 - 1」，故传 `16-1`）、类型 INCR。
- `axi_expect_wd_burst(16, Addr_v, 4, "1111", "1111", ...)`：接收 16 拍写数据；起始值为 `Addr_v`（即字节地址，这正是 OwnAddress pattern 的特征——数据 = 地址）；每拍数据递增 `4`（因为每拍前进 4 字节）；两个 `"1111"` 是写选通 strobe（4 字节全有效）。
- `axi_apply_bresp(xRESP_OKAY_c, ...)`：回送一个 OKAY 写响应，完成这次写事务。

读阶段循环结构对称，把 `axi_expect_aw` 换成 `axi_expect_ar`，把接收写数据换成回送读数据 `axi_apply_rresp_burst`：[tb/top_tb.vhd:L422-L427](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L422-L427)

```vhdl
Addr_v := 16#A8#;
while Addr_v < 16#1A8# loop
    axi_expect_ar      (Addr_v, AxSIZE_4_c, 16-1, xBURST_INCR_c, m_axi_ms, m_axi_sm, aclk);
    axi_apply_rresp_burst(16, Addr_v, 4, xRESP_OKAY_c, m_axi_ms, m_axi_sm, aclk);
    Addr_v := Addr_v + 16*4;
end loop;
```

读完后 `AxiDone <= 0`，唤醒 `p_control` 去校验结果：[tb/top_tb.vhd:L428](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L428)

不同 pattern 用不同的「起始数据 / 递增量」：

- OwnAddress：起始 = 字节地址，每拍 `+4`（[L418](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L418)）。
- Counter：起始 = 0，每拍 `+1`，每个 burst 起始再 `+16`（[L487](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L487)、[L490](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L490)）。
- Walking-1：用 testbench 本地定义的 `axi_expect_wd_walk1` / `axi_apply_rresp_walk1`（见 [L92-L154](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L92-L154)），实现「单个 1 循环左移」。

> 旁注：psi_tb 的 `axi_expect_wd_burst` / `axi_apply_rresp_burst` 只支持「等差数列」式数据（起始 + 固定增量），所以 Walking-1 这种非等差 pattern 不能用它，testbench 才自己写了两个本地过程（见 [L92-L123](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L92-L123) 与 [L125-L154](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L125-L154)）。这两个本地过程也是本讲综合实践「自定义单错误注入过程」的范本。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「地址递增 64 字节、每 burst 16 拍」与测试区域大小的换算关系。
2. **操作步骤**：用例 0 测试区域 0xA8 起、`SIZE=0x100`（256 字节）。计算应有多少个 burst、多少拍。
3. **需要观察的现象**：对照 [L415-L427](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L415-L427) 的循环上界 `< 16#1A8#`（= 0xA8 + 0x100）。
4. **预期结果**：256 字节 ÷ 4 字节/拍 = 64 拍；64 拍 ÷ 16 拍/burst = 4 个 burst；地址序列 0xA8、0xE8、0x128、0x168，正好停在 0x1A8 之前。
5. 纯算术，无需运行。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `axi_expect_aw` 的长度参数传 `16-1` 而不是 `16`？
  - **答案**：AXI4 的 `AWLEN`/`ARLEN` 字段表示「本次 burst 的拍数减 1」（即最后一个拍的下标）。要传 16 拍，字段值就是 15 = `16-1`。
- **练习 2**：在 `M00_AXI` 上，`m_axi_ms` 由谁驱动？
  - **答案**：由 **DUT** 驱动（DUT 是 master）。testbench 驱动 `m_axi_sm`（slave 侧，扮演存储器）。可在 DUT 端口映射里核对：`m00_axi_awvalid => m_axi_ms.awvalid`（[L221](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L221)）、`m00_axi_awready => m_axi_sm.awready`（[L222](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L222)）。

### 4.3 错误注入与结果校验

#### 4.3.1 概念说明

如果存储器永远返回正确数据，硬件的 `ERRORS` 永远是 0，我们就**无法验证**错误计数与首个错误地址逻辑是否正确。因此 testbench 必须能**故意投放错误**：在读响应里返回与 pattern 期望不符的数据，看硬件是否数对错误数、记对第一个错的地址。

`top_tb.vhd` 在用例 1、2、4 三处做了错误注入，手法都是「让某段 burst 的读数据递增量偏离正确值」。本节聚焦用例 1（OwnAddress、15 个错误、首个错误地址 0xEC）。

#### 4.3.2 核心流程

OwnAddress pattern 的正确读数据是「数据 = 字节地址」，每拍 `+4`。`p_axi` 通过一个 `Incr_v` 变量控制每拍递增量：

```text
读阶段（用例 1）：
  for 每个 burst:
      if 这个 burst 起始地址 == 0xE8:
          Incr_v := 1        -- 故意用错误递增量
      else:
          Incr_v := 4        -- 正确递增量
      axi_apply_rresp_burst(16, Addr_v, Incr_v, OKAY, ...)
```

于是从 0xE8 起的那个 burst，读数据变成了 `0xE8, 0xE9, 0xEA, ...`（每拍 `+1`），而 DUT 期望的是 `0xE8, 0xEC, 0xF0, ...`（每拍 `+4`）。逐拍比对的结果是：

- 第 1 拍：实际 0xE8 = 期望 0xE8 → **匹配**（两者都从 `Addr_v=0xE8` 起）。
- 第 2 拍：实际 0xE9 ≠ 期望 0xEC → **错误**，对应字节地址 0xEC（= 0xE8 + 4×(2−1)）。
- 第 3~16 拍：每拍都错 → 共 15 个错误，首个在 0xEC。

这正好命中 u3-l4 的换算公式：硬件用 `FirstErrAddr = 基地址 + PatternCnt×B` 算出首个错误地址，并在 `Read_s` 里累加 `Errors`。

#### 4.3.3 源码精读

错误注入的 `Incr_v` 选择，位于用例 1 的读阶段：[tb/top_tb.vhd:L441-L451](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L441-L451)

```vhdl
Addr_v := 16#A8#;
while Addr_v < 16#1A8# loop
    -- For the address block starting at 16#A8#+16*4 , use a wrong increment
    -- to produce 15 errors (first error at 0xEC)
    if Addr_v = 16#E8# then
        Incr_v := 1;
    else
        Incr_v := 4;
    end if;
    axi_expect_ar      (Addr_v, AxSIZE_4_c, 16-1, xBURST_INCR_c, m_axi_ms, m_axi_sm, aclk);
    axi_apply_rresp_burst(16, Addr_v, Incr_v, xRESP_OKAY_c, m_axi_ms, m_axi_sm, aclk);
    Addr_v := Addr_v + 16*4;
end loop;
```

注释里写明了意图：从 0xE8 起的 block 用错误递增量，制造 15 个错误、首个错误在 0xEC。注意 `axi_apply_rresp_burst` 的第三个参数正是每拍数据递增量，把它从 `4` 改成 `1` 就完成了注入——无需手写每拍数据。

`p_control` 侧对应的结果校验：期望 `ERRORS=15`、`FERR_ADDR_LO=0xEC`、`FERR_ADDR_HI=0`：[tb/top_tb.vhd:L310-L313](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L310-L313)

```vhdl
axi_single_expect(REG_STATUS*4,        C_STATUS_IDLE, s_axi_ms, s_axi_sm, aclk, "Status not idle 1");
axi_single_expect(REG_ERRORS*4,        15,            s_axi_ms, s_axi_sm, aclk, "Errors not found 1");
axi_single_expect(REG_FERR_ADDR_LO*4,  16#EC#,        s_axi_ms, s_axi_sm, aclk, "Error addr lo wrong 1");
axi_single_expect(REG_FERR_ADDR_HI*4,  0,             s_axi_ms, s_axi_sm, aclk, "Error addr hi wrong 1");
```

Walking-1 用例（用例 4）用的是另一种注入手法：在读阶段，遇到地址 0x1C0 的 burst 时把起始位 `Cnt_v` 额外 `+1`，使整段 burst 的「1」位置整体错位，产生 16 个错误、首个错误在 0x1C0：[tb/top_tb.vhd:L515-L522](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L515-L522)。两种手法的共同点是：**只改动仿真侧返回给 DUT 的数据，不动 DUT 本身**，从而把硬件的检错逻辑逼出来。

> Continuous 用例（用例 2）把用例 1 的写+读循环套在 `for i in 0 to 2 loop` 里跑 3 遍（[L457-L477](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L457-L477)），于是累计错误 `15*3=45`（[L333](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L333)），迭代计数 `ITER=3`（[L334](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L334)）。这同时验证了 u2-l2 提到的「Continuous 模式错误全程累计」。

#### 4.3.4 代码实践（综合实践在此节展开）

本讲的代码实践任务：**解释用例 1 的错误注入原理，并新增一个「只产生 1 个错误」的用例，校验 `FIRSTERR` 地址。**

**第一部分：解释用例 1（已在 4.3.2 推导）**

把结论再钉一遍：`Incr_v` 由 `4` 改为 `1`，使从 0xE8 起的 burst 读数据序列从「每拍 +4」变成「每拍 +1」；第 1 拍（0xE8）仍正确，第 2~16 拍共 15 拍错误，首个错误在第 2 拍、字节地址 0xEC。

**第二部分：新增单错误用例（示例代码，需本地验证）**

psi_tb 的 `axi_apply_rresp_burst` 只能整段 burst 用同一个递增量，无法「只翻一拍」。要产生**恰好一个**错误，需仿照本地过程 [axi_apply_rresp_walk1（L125-L154）](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L125-L154) 写一个自定义读响应过程，在第 `CorruptBeat` 拍把数据加一个偏移、其余拍正常。下面是示例代码（不是项目原有代码）：

```vhdl
-- 示例代码：仿照 axi_apply_rresp_walk1 写一个「仅某一拍出错」的 OwnAddress 读响应过程
procedure axi_apply_rresp_ownadd_oneerr(
        Beats       : in  natural;
        DataStart   : in  natural;   -- 该 burst 起始字节地址（= 第 1 拍期望数据）
        CorruptBeat : in  natural;   -- 要破坏的拍号（1..Beats）
        CorruptOfs  : in  natural;   -- 该拍数据的偏移量（非 0 即产生错误）
        Response    : in  std_logic_vector(1 downto 0);
        signal ms   : in  axi_ms_r;
        signal sm   : out axi_sm_r;
        signal aclk : in  std_logic) is
    variable DataStdlv_v : std_logic_vector(ms.rdata'range);
    variable DataInt_v   : integer;
begin
    sm.rvalid <= '1';
    sm.rlast  <= '0';
    sm.rresp  <= Response;
    DataInt_v := DataStart;
    for beat in 1 to Beats loop
        if beat = Beats then
            sm.rlast <= '1';
        end if;
        if beat = CorruptBeat then
            DataInt_v := DataInt_v + CorruptOfs;   -- 仅这一拍数据被改动
        end if;
        DataStdlv_v := std_logic_vector(to_unsigned(DataInt_v, DataStdlv_v'length));
        sm.rdata <= DataStdlv_v;
        wait until rising_edge(aclk) and ms.rready = '1';
        DataInt_v := DataInt_v + 4;                -- OwnAddress：每拍 +4 字节
        if beat = CorruptBeat then
            DataInt_v := DataInt_v - CorruptOfs;   -- 后续拍恢复正确序列
        end if;
        if not (beat = Beats) then
            sm.rvalid <= '1';
        end if;
    end loop;
    axi_slave_init(sm);
end procedure;
```

然后在 `p_control` 与 `p_axi` 各加一段（示例代码，编号定为 7）。设计目标：在从 0xA8 起的第一个 burst 里，破坏第 5 拍，于是唯一错误落在字节地址 \(0xA8 + 4\times(5-1) = 0xB8\)。

`p_control` 末尾（在 `TbRunning <= false` 之前）追加：

```vhdl
-- 示例代码：新增单错误用例
print(">> Single Error at 0xB8");
axi_single_write(REG_MODE*4,        C_MODE_SINGLE,        s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_SIZE_LO*4,     16#100#,              s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_PATTERN_SEL*4, C_PATTERN_SEL_OWNADD, s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_ADDR_LO*4,     16#A8#,               s_axi_ms, s_axi_sm, aclk);
axi_single_write(REG_START*4,       1,                    s_axi_ms, s_axi_sm, aclk);
SetupDone <= 7;
wait until AxiDone = 7;
wait until rising_edge(aclk);
axi_single_expect(REG_STATUS*4,       C_STATUS_IDLE, s_axi_ms, s_axi_sm, aclk, "Status not idle 7");
axi_single_expect(REG_ERRORS*4,       1,              s_axi_ms, s_axi_sm, aclk, "Errors not 1");
axi_single_expect(REG_FERR_ADDR_LO*4, 16#B8#,         s_axi_ms, s_axi_sm, aclk, "Error addr lo wrong 7");
axi_single_expect(REG_FERR_ADDR_HI*4, 0,              s_axi_ms, s_axi_sm, aclk, "Error addr hi wrong 7");
```

`p_axi` 末尾（在最后的 `wait;` 之前）追加：

```vhdl
-- 示例代码：仿真单错误用例的存储器
wait until SetupDone = 7;
wait until rising_edge(aclk);
-- 写阶段：正常
Addr_v := 16#A8#;
while Addr_v < 16#1A8# loop
    axi_expect_aw      (Addr_v, AxSIZE_4_c, 16-1, xBURST_INCR_c, m_axi_ms, m_axi_sm, aclk);
    axi_expect_wd_burst(16, Addr_v, 4, "1111", "1111", m_axi_ms, m_axi_sm, aclk);
    axi_apply_bresp    (xRESP_OKAY_c, m_axi_ms, m_axi_sm, aclk);
    Addr_v := Addr_v + 16*4;
end loop;
-- 读阶段：第一个 burst 注入单错误，其余正常
Addr_v := 16#A8#;
while Addr_v < 16#1A8# loop
    axi_expect_ar(Addr_v, AxSIZE_4_c, 16-1, xBURST_INCR_c, m_axi_ms, m_axi_sm, aclk);
    if Addr_v = 16#A8# then
        axi_apply_rresp_ownadd_oneerr(16, Addr_v, 5, 1, xRESP_OKAY_c, m_axi_ms, m_axi_sm, aclk);
    else
        axi_apply_rresp_burst(16, Addr_v, 4, xRESP_OKAY_c, m_axi_ms, m_axi_sm, aclk);
    end if;
    Addr_v := Addr_v + 16*4;
end loop;
AxiDone <= 7;
```

1. **实践目标**：让硬件在「恰好 1 拍读数据错误」的场景下，把 `ERRORS` 计为 1、把 `FIRSTERR` 记为 0xB8。
2. **操作步骤**：把上述自定义过程加入 `architecture sim is` 的过程定义区（与 [L92-L154](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L92-L154) 并列），再在两进程末尾追加对应段落，按 u1-l3 的方法在 `sim/` 下 `source ./run.tcl` 跑回归。
3. **需要观察的现象**：Transcript 中新用例的 `axi_single_expect` 全部不报 `###ERROR###`，结尾出现 `SIMULATIONS COMPLETED SUCCESSFULLY`。
4. **预期结果**：`ERRORS=1`、`FERR_ADDR_LO=0xB8`。若把 `CorruptBeat` 改成别的值，`FERR_ADDR_LO` 应等于 \(0xA8 + 4\times(\text{CorruptBeat}-1)\)。
5. **待本地验证**：本环境未安装 Modelsim 与 psi_tb/psi_common 依赖，以上为新编写示例代码，作者未实际运行；请在装有依赖的环境中验证。

#### 4.3.5 小练习与答案

- **练习 1**：如果把用例 1 的 `Incr_v := 1` 改成 `Incr_v := 0`（读数据完全不变），会产生多少个错误？首个错误地址是多少？
  - **答案**：从 0xE8 起的 burst，实际数据变为全 `0xE8`；第 1 拍仍匹配（期望 0xE8），第 2~16 拍期望 `0xEC, 0xF0, ...` 全部不符 → 仍是 15 个错误，首个错误仍在第 2 拍、地址 0xEC。错误数与首错地址不变，只是「错法」不同。
- **练习 2**：为什么错误注入只发生在**读阶段**，而不在写阶段动手脚？
  - **答案**：写阶段是 DUT 把 pattern 写进（仿真）存储器，硬件不会在写阶段检错；检错只发生在读阶段——DUT 读回数据并与本地重新生成的 pattern 比对（见 u3-l4 的 `Read_s`）。所以只有篡改读响应才能触发 `Errors` 累加与 `FirstErrAddr` 记录。

## 5. 综合实践

把本讲三块内容串成一个任务：**为 `mem_test` 增加一个回归用例，验证「Counter pattern + 单个错误」**。

1. 选定 pattern 为 Counter（`C_PATTERN_SEL_COUNT`），模式 SINGLE，区域 0xA8 起、`SIZE=0x100`。
2. 在 `p_axi` 里，Counter 的正确读数据是「起始 0、每拍 +1、跨 burst 累计」（参考 [L492-L499](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L492-L499)）。仿照 4.3.4 的自定义过程，写一个适用于 Counter 的「单拍出错」读响应过程（提示：把每拍 `+1` 改成在 `CorruptBeat` 拍 `+1+CorruptOfs`）。
3. 在第一个 burst（0xA8 起）的第 8 拍注入一个错误，推演期望的 `FIRSTERR` 地址：Counter pattern 下，第 \(k\) 拍覆盖字节地址 \(0xA8 + 4(k-1)\)，第 8 拍即 \(0xA8 + 4\times 7 = 0xC4\)。
4. 在 `p_control` 用 `axi_single_expect` 断言 `ERRORS=1`、`FERR_ADDR_LO=0xC4`、`FERR_ADDR_HI=0`、`STATUS=IDLE`。
5. 跑 `source ./run.tcl`，确认无 `###ERROR###`（待本地验证）。

这个任务要求你同时用到：寄存器配置（4.1）、AXI4 仿真响应（4.2）与错误注入（4.3），并交叉验证 u3-l4 的首错地址换算公式。

## 6. 本讲小结

- `top_tb` 用两个并发进程分工：`p_control` 当 CPU 配置寄存器并校验结果，`p_axi` 当被测存储器响应突发读写；二者靠 `SetupDone` / `AxiDone` 两个整数信号做「配置 → 仿真 → 校验」的逐用例握手。
- 控制面 `S00_AXI` 上 TB 是 master、DUT 是 slave；数据面 `M00_AXI` 上 DUT 是 master、TB 是 slave——两组记录信号 `*_axi_ms` / `*_axi_sm` 的驱动方向正好相反，是阅读本 TB 的关键。
- psi_tb 的 `axi_single_write/read/expect` 封装了 AXI-Lite 单拍访问，`axi_expect_aw/ar` 校验命令、`axi_expect_wd_burst`/`axi_apply_rresp_burst` 处理等差数列式的 burst 数据，`axi_apply_bresp`/`axi_slave_init` 收尾事务。
- 错误注入的核心手法是「改读响应里每拍数据的递增量 `Incr_v`」：用例 1 把 0xE8 起 burst 的 `Incr_v` 从 4 改成 1，制造 15 个错误、首错地址 0xEC，正好验证 u3-l4 的 `Errors` 计数与 `FirstErrAddr` 换算。
- 要产生「恰好一个错误」需自定义读响应过程（仿照本地 `axi_apply_rresp_walk1`），因为库过程只能整段 burst 用同一递增量。
- 所有失败都通过 `assert ... report "###ERROR### ..."` 宣告，与 u1-l3 的 CI 判定约定衔接。

## 7. 下一步学习建议

- 下一讲 **u5-l2 Vivado IP 封装流程** 会离开仿真、进入 `scripts/package.tcl` 与 `xgui`，看这份 testbench 连同 RTL 是如何被打包成可分发的 IP 核的。
- 若想更扎实地理解本讲的 AXI 校验细节，建议补读外部库 `psi_tb` 的 `psi_tb_axi_pkg.vhd`（不在本仓库，需按 u1-l2 的依赖获取方式拉取），对照 `axi_expect_wd_burst` / `axi_apply_rresp_burst` 的真实实现。
- 想加深对「错误为何这样计数」的理解，可回看 u3-l4 的 `Read_s` 比对逻辑与首个错误地址换算，再回到本讲用例 1 双向印证。
