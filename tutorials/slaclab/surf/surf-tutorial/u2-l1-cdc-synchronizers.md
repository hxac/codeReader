# 时钟域跨越与同步器（base/sync）

## 1. 本讲目标

本讲聚焦 SURF 里出现频率最高的一类问题：**信号在两个互不相关的时钟域之间怎么安全搬运**。

读完本讲，你应当能够：

- 说清楚「直接把一个时钟域的信号接到另一个时钟域」为什么会让触发器进入**亚稳态**，以及多级同步器如何把这个问题压到可以忽略。
- 读懂并会用 `base/sync` 同步器族里的四个核心模块：`Synchronizer`、`SynchronizerOneShot`、`RstSync`、`SyncStatusVector`，并知道每种分别解决哪一类跨域问题。
- 理解 `SynchronizerOneShot` 为什么能把「慢域一个脉冲」可靠地变成「快域恰好一个时钟周期的脉冲」，并能据此写出一个跨域触发的实例。
- 看懂同步器上那一大串综合属性（`ASYNC_REG`、`shreg_extract` 等）到底在保护什么。

本讲承接 [u1-l5 双进程 RTL 风格](u1-l5-two-process-style.md)：这里出现的 `comb`/`seq`、`RegType`/`REG_INIT_C`、`RST_ASYNC_G` 都来自那一讲，本讲不再重复其写法，只聚焦「跨域」这个新维度。

## 2. 前置知识

### 2.1 什么是时钟域

一块 FPGA 里常常同时跑好几个时钟：125 MHz 的以太网时钟、200 MHz 的 DDR 时钟、几十 MHz 的慢速管理时钟…… 由**同一个时钟的上升沿**采样的所有触发器，属于同一个「时钟域（clock domain）」。只要两个时钟不是同源、或频率/相位关系不被设计约束锁定，就认为它们是**异步**的，它们各自构成独立的时钟域。

### 2.2 亚稳态（metastability）

触发器在采样时，如果输入数据在建立/保持时间窗口内发生跳变，输出就可能既不是干净的 0 也不是干净的 1，而是停留在某个中间电平上，过一段**不确定的时间**才随机塌缩到 0 或 1。这种状态叫亚稳态。

单个触发器从亚稳态恢复所需的时间是随机的，但统计上「恢复概率」随时间指数下降。工程上量化它的经典公式是平均无故障时间：

\[
\text{MTBF} \approx \frac{e^{\,t_r/\tau}}{T_0 \cdot f_{\text{clk}} \cdot f_{\text{data}}}
\]

其中 \(t_r\) 是留给触发器恢复的「解析时间」，\(\tau\)、\(T_0\) 是工艺常数，\(f_{\text{clk}}\) 与 \(f_{\text{data}}}\) 分别是采样时钟与数据翻转频率。关键结论是分子里的指数项：**给越多的恢复时间（多串几级触发器），MTBF 呈指数级上升**。串两级触发器，MTBF 可能从「几毫秒」变成「几万年」——这就是同步器存在的全部理由。

### 2.3 同步器的三条铁律

把上面两点合起来，就有 SURF 全仓库遵守的三条约定：

1. **跨域信号必须是单比特**，或者已经握手好的「单比特使能 + 多比特数据」。多比特总线绝不能逐位各拍一级同步器——各位到达时间不一致，采样到的会是「撕裂」的错位值。多比特跨域交给 [u2-l2 FIFO](u2-l2-fifo-blocks.md) 的异步 FIFO 解决。
2. **目的域里串至少 2 级触发器**，且综合工具不许对这条链做任何优化（不许折成 SRL、不许寄存器平衡、不许插入逻辑）。
3. **脉冲型信号要先变事件**：如果一个脉冲比目的时钟周期还窄，普通同步器可能整个漏采；如果比目的周期宽很多，又会被当成一串事件。这类信号要用边沿检测型同步器。

本讲的四个模块，就是这三条铁律的具体实现。

## 3. 本讲源码地图

| 文件 | 角色 | 一句话作用 |
|------|------|-----------|
| [base/sync/rtl/Synchronizer.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd) | rtl（可综合） | 单比特多级触发器同步器，是整个族的原子积木 |
| [base/sync/rtl/SynchronizerVector.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerVector.vhd) | rtl | 把 `Synchronizer` 按位复制成一条向量（**仅适用于彼此独立的各比特**） |
| [base/sync/rtl/SynchronizerEdge.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerEdge.vhd) | rtl | 在同步器后接一级延迟寄存器，输出上升沿/下降沿单拍选通 |
| [base/sync/rtl/SynchronizerOneShot.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd) | rtl | 把异步触发压成目的域里恰好一周期的脉冲（**本讲主角**） |
| [base/sync/rtl/RstSync.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/RstSync.vhd) | rtl | 「异步置位、同步撤销」的复位同步器 |
| [base/sync/rtl/SyncStatusVector.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SyncStatusVector.vhd) | rtl | 综合件：状态电平同步 + 事件计数 + 中断聚合 |
| [base/sync/rtl/SynchronizerOneShotCntVector.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShotCntVector.vhd) | rtl | `SyncStatusVector` 的「每比特事件计数」子件，含计数总线跨域 |
| [tests/base/sync/test_Synchronizer.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/sync/test_Synchronizer.py) | cocotb 测试 | 验证多级同步器的「延迟 = 级数」 |
| [tests/base/sync/test_SynchronizerOneShot.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/sync/test_SynchronizerOneShot.py) | cocotb 测试 | 验证单拍/展宽脉冲宽度与复位行为 |

整个 `base/sync` 目录的 `ruckus.tcl` 只有两行：把 `rtl/` 当综合源加载、把 `tb/` 当 `sim_only` 加载，印证 [u1-l3](u1-l3-directory-layout.md) 讲的「用目录名表达文件角色」约定（见 [base/sync/ruckus.tcl:L4-L8](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/ruckus.tcl#L4-L8)）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应三类跨域问题：

- **4.1 多级同步器**：把一个**电平**（慢变的高低状态）安全搬到目的域。主角 `Synchronizer` / `SynchronizerVector`。
- **4.2 脉冲/边沿跨域**：把一个**事件**（一次触发、一个脉冲）安全搬到目的域。主角 `SynchronizerOneShot`（内部用 `SynchronizerEdge`）。
- **4.3 复位同步与状态同步**：让一个时钟域的**复位**安全撤销，以及把一整组状态位连同「翻转次数」一起搬到目的域。主角 `RstSync` / `SyncStatusVector`。

### 4.1 多级同步器（Synchronizer / SynchronizerVector）

#### 4.1.1 概念说明

`Synchronizer` 解决最朴素的问题：源域里有一个**慢变的高低电平**（比如一个「链路已锁定」标志位），目的域想看它的当前值。直接拉一根线过去会踩亚稳态（见 2.2），于是我们在目的域串一串触发器，给亚稳态足够长的解析时间，最后一级再当成稳定值用。

注意它只适合**电平**——也就是信号跳变频率远低于目的时钟，且每次跳变后能稳定住很长时间。它**不保证**捕捉每一次跳变：如果输入在两个采样沿之间跳了好几次，输出只会看到最后一次的值。捕捉「事件」要等到 4.2。

`SynchronizerVector` 是同一个东西的向量版：对一条 `WIDTH_G` 位的输入，**每一位各自独立**地做一条同步链。务必记住「各自独立」——它适合一束互不相关的状态标志位，**绝不**适合一个需要整体一致的数值（例如一个 16 位计数器），否则各比特到达时间错开，采样到的就是个错位的乱码。

#### 4.1.2 核心流程

`Synchronizer` 的数据流非常简单，是一条移位寄存器链：

```text
dataIn ──▶ [FF1] ──▶ [FF2] ──▶ ... ──▶ [FF{STAGES_G}] ──▶ dataOut
            ↑ 每一级都在 clk(目的域) 的上升沿采样
            ↑ 第 1 级最可能亚稳态, 后面每一级给它一整个周期去恢复
```

每一拍，整条链左移一位、最低位吃进新输入：

```text
rin <= crossDomainSyncReg(STAGES_G-2 downto 0) & dataIn
```

输出取最高位（最老的那一级），并可选地按 `OUT_POLARITY_G` 取反。

#### 4.1.3 源码精读

先看实体声明，体会 SURF 标准的那组泛型（[base/sync/rtl/Synchronizer.vhd:L22-L36](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L22-L36)）：

```vhdl
entity Synchronizer is
   generic (
      TPD_G          : time     := 1 ns;
      RST_POLARITY_G : sl       := '1';   -- '1'=高有效复位, '0'=低有效
      OUT_POLARITY_G : sl       := '1';   -- 输出是否取反
      RST_ASYNC_G    : boolean  := false; -- 复位走异步还是同步
      STAGES_G       : positive := 2;     -- 同步链级数(>=2)
      BYPASS_SYNC_G  : boolean  := false; -- 同源时直通, 省延迟
      INIT_G         : slv      := "0");  -- 各级上电初值
   port (
      clk     : in  sl;
      rst     : in  sl := not RST_POLARITY_G;
      dataIn  : in  sl;
      dataOut : out sl);
end Synchronizer;
```

几个要点：

- `STAGES_G` 默认 2，是同步器的「最小安全级数」；代码里有一条硬断言卡住下限（[L78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L78)）。
- `BYPASS_SYNC_G` 留给「其实两边是同一个时钟」的情况——这时不需要同步，直接连线省掉 `STAGES_G` 拍延迟（见后文 `BYPASS` 分支）。
- `INIT_G` 让你能定义链的上电值；用 `ite` 把缺省值 `"0"` 展开成全 0 的常量 `INIT_C`（[L40](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L40)）。

整条链就是一条 `slv` 信号 `crossDomainSyncReg`（[L42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L42)）。**本模块最关键、也最容易被初学者忽略的一段**，是它后面挂的一长串综合属性（[base/sync/rtl/Synchronizer.vhd:L49-L74](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L49-L74)）：

```vhdl
attribute ASYNC_REG                       : string;
attribute ASYNC_REG of crossDomainSyncReg : signal is "TRUE";
...
attribute shreg_extract   of crossDomainSyncReg : signal is "no";  -- 不许折成 SRL
attribute register_balancing of crossDomainSyncReg : signal is "no";-- 不许寄存器平衡
attribute altera_attribute of crossDomainSyncReg : signal is "-name AUTO_SHIFT_REGISTER_RECOGNITION OFF";
```

它们的存在只有一个目的：**钉死这条链，不让综合工具动它**。具体地：

- `ASYNC_REG = "TRUE"` 告诉 Vivado 这些触发器采的是异步数据，应当被放在同一 SLICE 里紧挨着、且中间不许插逻辑——这是 Vivado 做跨域约束（`set_false_path` / `ASYNC_REG` 流程）能识别的标记。
- `shreg_extract = "no"`、`syn_srlstyle = "registers"`、Altera 的 `AUTO_SHIFT_REGISTER_RECOGNITION OFF`：禁止工具把这条移位链「优化」成一个 SRL（移位寄存器查找表）。SRL 看起来省资源，但它**不是真正的多级触发器**，没有给亚稳态逐级恢复的时间，会毁掉同步器的意义。
- `register_balancing = "no"`、`MSGON = "FALSE"`：不许工具在链之间搬移/合并寄存器，也不让反标仿真对它报时序违例（因为它本就是跨域的「故意违例」点）。

> 读源码时要建立直觉：**看到一堆 `attribute ... of ... is` 围着某条信号，往往就是跨域同步链**。这是 SURF 里识别 CDC 边界最快的方法。

组合进程 `comb` 只做移位和输出极性选择（[L82-L92](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L82-L92)）：

```vhdl
comb : process (crossDomainSyncReg, dataIn) is
begin
   rin <= crossDomainSyncReg(STAGES_G-2 downto 0) & dataIn;  -- 左移吃入新输入
   if (OUT_POLARITY_G = '1') then
      dataOut <= crossDomainSyncReg(STAGES_G-1);              -- 取最高位
   else
      dataOut <= not(crossDomainSyncReg(STAGES_G-1));
   end if;
end process comb;
```

时序进程 `seq` 分成 `ASYNC_RST` / `SYNC_RST` 两套 `generate`，由 `RST_ASYNC_G` 二选一——这正是 [u1-l5](u1-l5-two-process-style.md) 讲的「同一泛型互斥切换两条复位路径」（[L94-L117](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L94-L117)）。注意：`Synchronizer` 本身的复位处理的是「这条链在复位时清成 `INIT_C`」，与「跨域数据的亚稳态」是两件事。

最后，`BYPASS_SYNC_G = true` 时直接连线（[L121-L125](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/Synchronizer.vhd#L121-L125)），所以同一个模块既能用于真异步，也能用于已知同源的场景，只换一个泛型即可。

`SynchronizerVector` 的实现就是把上面这套逻辑套进一个 `for ... generate`，对每一位各做一条链（[base/sync/rtl/SynchronizerVector.vhd:L100-L111](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerVector.vhd#L100-L111)）。它的同步属性、`BYPASS`、`ASYNC_RST`/`SYNC_RST` 分支都与单比特版一字不差，只是作用在 `RegArray`（二维：位 × 级）上。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「多级同步器的输出延迟正好等于级数 `STAGES_G`」。

**操作步骤**（依赖 [u1-l2](u1-l2-build-sim-toolchain.md) 讲的工具链）：

1. 在仓库根目录先做一次源码缓存（若已做过可跳过）：`make MODULES=$PWD import`。
2. 跑 `Synchronizer` 的 cocotb 回归：

   ```bash
   ./.venv/bin/python -m pytest -q tests/base/sync/test_Synchronizer.py
   ```

3. 打开 [tests/base/sync/sync_test_utils.py:L91-L108](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/sync/sync_test_utils.py#L91-L108) 阅读关键协程 `drive_and_expect_after_latency`：它先记下旧输出，改变 `dataIn`，然后断言「在到达最后一级之前，输出仍是旧值」，直到第 `STAGES_G` 拍才出现新值。

**需要观察的现象**：测试通过即说明输出在新输入到达后的前 `STAGES_G-1` 拍保持不变，第 `STAGES_G` 拍才更新。

**预期结果**：测试用例全绿。如果想更直观，可在 `drive_and_expect_after_latency` 里把 `self.stages` 换成不同的 `STAGES_G`（经 `PARAMETER_SWEEP` 的 `parameter_case(..., STAGES_G="4")`），观察延迟随之变成 4 拍。

> 本实践依赖本机已配好 GHDL + cocotb 环境；若尚未配置，按 u1-l2 的 `pip_requirements.txt` 安装后重试，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Synchronizer` 上要加 `shreg_extract = "no"`？

> **答**：不加的话，综合工具可能把这条多级触发器链折叠成一个 SRL（移位寄存器 LUT）。SRL 表面上是「移位」，但内部是查找表而非独立触发器，给亚稳态的恢复时间不再是「每级一个完整时钟周期」，MTBF 会急剧恶化，等于把同步器废掉了。

**练习 2**：`SynchronizerVector` 能不能用来跨域搬运一个 16 位的「当前温度值」？为什么？

> **答**：不能。`SynchronizerVector` 对每一位独立同步，各位到达输出端的时间可能错开一拍，目的域会采样到「一半新值一半旧值」的撕裂结果。正确做法是用握手或 [u2-l2](u2-l2-fifo-blocks.md) 的异步 FIFO 把整个字原子地搬过去。

---

### 4.2 脉冲/边沿跨域（SynchronizerOneShot / SynchronizerEdge）

#### 4.2.1 概念说明

很多跨域需求传递的不是「电平」而是「事件」：源域发生了一次「采样完成」「门打开了一次」「收到一个触发」——它表现为一个脉冲。把脉冲跨域有两大坑：

1. **脉冲太窄被漏采**：如果脉冲宽度小于一个目的时钟周期，目的域的采样沿很可能恰好落在脉冲之外，整个事件消失。
2. **脉冲太宽被当成多次**：如果脉冲持续了好几个目的周期（比如慢时钟 1 MHz、快时钟 125 MHz，慢域高电平 1 µs = 125 个快周期），直接同步过去会被下游误认成一串 125 个事件。

`SynchronizerOneShot` 一举解决这两点：无论输入脉冲多宽多窄，它都在目的域输出**恰好一个时钟周期**的脉冲（需要更宽时可设 `PULSE_WIDTH_G` 展宽）。它是 SURF 里「跨域触发」的事实标准，也是本讲实践任务的主角。

它内部用了两个积木：

- `SynchronizerEdge`：在普通同步器之后多加一级延迟寄存器，比较「当前同步值」与「上一拍同步值」，输出上升沿/下降沿的单拍选通。
- `RstSync`：4.3 要讲的复位同步器。`SynchronizerOneShot` 灵活地把 `RstSync` 当成「把一个慢/异步电平整干净」的工具来用。

#### 4.2.2 核心流程

`SynchronizerOneShot`（非 bypass 模式）的内部流水如下：

```text
dataIn(任意宽度脉冲, 源域)
   │
   ▼  RstSync: 同步 + 去毛刺, 得到一个干净电平 pulseRst(目的域)
   │  (脉冲很窄时, RstSync 的复位路径会把有效电平"按住", 保证不被漏采)
   ▼  SynchronizerEdge: 多级同步 + 上升沿检测 → edgeDet(单拍)
   │
   ▼  PULSE_WIDTH_G=1 ? dataOut=edgeDet : 用计数器展宽到 PULSE_WIDTH_G 拍
dataOut(目的域恰好 N 拍脉冲)
```

为什么用 `RstSync` 做第一级？因为 `RstSync` 内部本来就是为了「把一个异步的复位电平安全同步进来」而设计的，它天然能处理「输入是一个时高时低、可能很宽的电平」这种情况，并保证输出在目的域里是一个干净、稳定的电平。把它接在 `dataIn` 上，相当于先把脉冲整形成一个稳定电平；再交给 `SynchronizerEdge` 检测这个电平的上升沿，就得到恰好一拍的事件。

#### 4.2.3 源码精读

实体声明（[base/sync/rtl/SynchronizerOneShot.vhd:L21-L36](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L21-L36)）相比 `Synchronizer` 多了两个关键泛型：

```vhdl
OUT_DELAY_G    : positive := 3;   -- 输出同步链级数(>=3)
PULSE_WIDTH_G  : positive := 1;   -- 输出脉冲宽度(目的域时钟周期数)
```

注意 `OUT_DELAY_G` 默认 3（不是 2），代码里有一条断言卡住下限（[L60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L60)）。原因是它要喂给 `SynchronizerEdge`，而后者要求 `STAGES_G >= 3`（见下文）。

整条链路在架构体里分三段。**第一段**：`BYPASS_SYNC_G = false` 时，用 `RstSync` 把 `dataIn` 整形成干净电平 `pulseRst`（[base/sync/rtl/SynchronizerOneShot.vhd:L66-L76](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L66-L76)）：

```vhdl
RstSync_Inst : entity surf.RstSync
   generic map (
      TPD_G          => TPD_G,
      IN_POLARITY_G  => IN_POLARITY_G,   -- 按输入极性识别"有效"
      OUT_POLARITY_G => '1')
   port map (
      clk      => clk,
      asyncRst => dataIn,     -- 慢/异步脉冲当作"复位源"喂进去
      syncRst  => pulseRst);  -- 目的域里干净、同步好的电平
```

`BYPASS_SYNC_G = true` 时则直接 `pulseRst <= dataIn`（[L62-L64](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L62-L64)），用于已知同源、只想要边沿检测的场景。

**第二段**：用 `SynchronizerEdge` 检测 `pulseRst` 的上升沿，得到 `edgeDet`（[base/sync/rtl/SynchronizerOneShot.vhd:L78-L91](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L78-L91)）。`SynchronizerEdge` 的实体声明里有 `risingEdge` / `fallingEdge` 两个单拍输出端口（[base/sync/rtl/SynchronizerEdge.vhd:L30-L37](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerEdge.vhd#L30-L37)），它内部先例化一个 `STAGES_G-1` 级的 `Synchronizer` 把数据整稳，再用一个状态记录 `syncDataDly` 做边沿比较（[base/sync/rtl/SynchronizerEdge.vhd:L99-L107](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerEdge.vhd#L99-L107)）：

```vhdl
if (syncData = '1') and (r.syncDataDly = '0') then
   v.risingEdge := OUT_POLARITY_G;     -- 本拍=1且上一拍=0 → 上升沿
end if;
```

这也解释了为什么 `SynchronizerEdge`（以及依赖它的 `SynchronizerOneShot`）要求级数 `>= 3`：`(STAGES_G-1)` 级给同步、留 1 级做 `syncDataDly` 延迟比较，总共至少 3。

**第三段**：根据 `PULSE_WIDTH_G` 决定要不要展宽。`=1` 时直接 `dataOut <= edgeDet`（[L92-L94](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L92-L94)）；`>1` 时用一个标准的 `comb`/`seq` 双进程计数器把脉冲撑到 `PULSE_WIDTH_G` 拍（[base/sync/rtl/SynchronizerOneShot.vhd:L100-L137](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L100-L137)）。这里的 `comb` 进程就是 [u1-l5](u1-l5-two-process-style.md) 的模板：`variable v := r` 起手、中间改 `v`、末尾 `rin <= v`，并按 `RST_ASYNC_G` 在 `comb` 里做同步复位分支：

```vhdl
if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then
   v := REG_INIT_C;
end if;
```

而 `seq` 进程则只管「异步复位在前、上升沿寄存在后」（[L130-L137](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L130-L137)），完全是 u1-l5 的三明治骨架。

#### 4.2.4 代码实践

**实践目标**：写一段 `SynchronizerOneShot` 实例，把慢时钟域的一个触发脉冲送到快时钟域，并写一段注释解释「为什么不能直接用普通信号传递」。这是本讲规格里指定的实践任务。

**操作步骤**：

1. 阅读现成的 cocotb 测试 [tests/base/sync/test_SynchronizerOneShot.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/sync/test_SynchronizerOneShot.py)，重点看它的 `pulse_width_test`：它先驱动一个触发，再连续采样输出，最后用 `_active_run_lengths` 数出输出里有且仅有「一个长度等于 `PULSE_WIDTH_G` 的脉冲」（[L120-L135](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/sync/test_SynchronizerOneShot.py#L120-L135)）。这正好印证「无论输入多宽，输出恰好 N 拍」。
2. （可选）运行该测试观察通过：`./.venv/bin/python -m pytest -q tests/base/sync/test_SynchronizerOneShot.py`。
3. 在你自己的顶层里加入下面的实例（**示例代码**，非仓库原有文件）：

   ```vhdl
   -- 示例代码: 把慢时钟域(slowClk, ~1 MHz)的"启动采集"脉冲 startPulse,
   --           送到快时钟域(fastClk, ~125 MHz)做成单拍事件 fastTrig。
   --
   -- 为什么不能直接写  fastTrig <= startPulse;  ?
   --   (1) 亚稳态: slowClk 与 fastClk 互不同源, startPulse 相对 fastClk 的跳变沿
   --       是异步的, 极易违反建立/保持时间, 第一级触发器会进入亚稳态, 输出悬空
   --       若干 ns 才随机塌缩到 0/1, 污染下游.
   --   (2) 漏采/多采: startPulse 在慢域若比一个 fastClk 周期还窄, 直接搬可能被
   --       完全漏采; 若它持续很多个 fastClk 周期, 又会被当成一长串事件.
   --   (3) 撕裂: 若是多比特, 各位到达时间不一致(本例只处理单比特).
   --   SynchronizerOneShot 用 "RstSync 整形 + SynchronizerEdge 边沿检测" 一次性
   --   解决上述问题: 输出在 fastClk 域稳定, 且无论输入持续多久都只产生恰好 1 拍脉冲.
   U_OneShot : entity surf.SynchronizerOneShot
      generic map (
         TPD_G          => TPD_G,
         RST_ASYNC_G    => RST_ASYNC_G,
         RST_POLARITY_G => RST_POLARITY_G,
         BYPASS_SYNC_G  => false,   -- 两时钟不同源, 必须真正同步
         IN_POLARITY_G  => '1',     -- 慢域脉冲高有效
         OUT_POLARITY_G => '1',     -- 快域输出高有效
         OUT_DELAY_G    => 3,       -- 同步链级数, 越大 MTBF 越好
         PULSE_WIDTH_G  => 1)       -- 只要一个 fastClk 周期的脉冲
      port map (
         clk     => fastClk,        -- 目的(快)时钟
         rst     => fastRst,        -- 快域同步复位
         dataIn  => startPulse,     -- 慢域来的触发
         dataOut => fastTrig);      -- fastClk 域的单拍事件
   ```

**需要观察的现象**：

- 即便 `startPulse` 在慢域保持了好几个 µs，`fastTrig` 每次触发只亮一个 `fastClk` 周期。
- 连续两次触发之间，只要间隔大于 `OUT_DELAY_G` 个快周期，就能各自得到独立的一拍脉冲。

**预期结果**：行为符合上面两点。综合后查看布局，`U_OneShot` 内部的同步链触发器应被 `ASYNC_REG` 约束、紧挨放置。具体的波形时序**待本地验证**（取决于你给 `slowClk`/`fastClk` 选的实际频率）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `SynchronizerOneShot` 的 `OUT_DELAY_G` 设成 2 会怎样？

> **答**：会在 elaboration 时触发断言失败（`OUT_DELAY_G must be >= 3`，[L60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShot.vhd#L60)），仿真直接报 `severity failure` 停下。因为 `OUT_DELAY_G` 透传给 `SynchronizerEdge` 的 `STAGES_G`，后者要求至少 3 级（同步链 + 边沿延迟寄存器）。

**练习 2**：`SynchronizerOneShot` 里 `RstSync` 被接在 `dataIn` 上，而不是接在复位网上，这种「挪用」为什么可行？

> **答**：`RstSync` 的本质是「把一个异步电平安全地同步进目的域，并保证它的撤销是干净的」。复位只是这种语义的一个特例。把它接到 `dataIn` 上，就是借用同一套机制把「脉冲电平」整形成目的域里干净、不漏采、不亚稳的电平，再由后级的边沿检测把它变成事件。这是 SURF 里很典型的「用一个成熟 CDC 原语搭出另一个」的复用思路。

---

### 4.3 复位同步与状态同步（RstSync / SyncStatusVector）

#### 4.3.1 概念说明

**复位同步**：一个时钟域的复位如果是异步来的，它的「撤销沿」对目的域就是异步的——这会导致域内不同触发器在不同拍释放复位，行为不确定。正确做法是「**异步置位、同步撤销（async assert, sync deassert）**」：进入复位要立刻生效（哪怕没有时钟，保证可靠复位），但退出复位必须同步到时钟，让所有触发器在同一拍醒来。这正是 `RstSync` 干的事，它也是每个 SURF 时钟域里生成本地复位的标配。

**状态同步**：实际工程里，跨域的往往不止一个标志位，而是一整组状态（每个通道的「锁定/出错/忙」），而且还想知道**每个标志位翻转了多少次**（事件计数），并据此产生中断。`SyncStatusVector` 把这些需求打包成一个综合件：电平同步 + 事件计数 + 中断聚合，三合一。

#### 4.3.2 核心流程

`RstSync` 的核心思想可以用伪代码概括：

```text
asyncRst(异步复位, 任意域)
   │
   ▼  复用 Synchronizer(STAGES=RELEASE_DELAY_G-1, RST_ASYNC_G=true, INIT=全"1")
   │    - 复位有效期间: 整条链被异步清成 INIT=全"1"(= 复位有效值), 输出立刻拉高
   │    - 复位撤销后: "0"(无效值)开始逐级移入, 经 RELEASE_DELAY_G-1 拍到达输出
   ▼  最终输出寄存器 OUT_REG
   │    - 异步置位: asyncRst 一有效, 输出立即变复位值(不等时钟)
   │    - 同步撤销: 输出只在时钟沿跟随 syncInt 变化 → 复位"撤销"被对齐到时钟
syncRst(目的域里干净的复位)
```

`SyncStatusVector` 的结构则是「两个并行子件 + 一个中断组合进程」：

```text
statusIn(W 位, wrClk 域)
   ├──▶ SynchronizerVector       ──▶ statusOut(W 位, rdClk 域)   [电平同步]
   └──▶ SynchronizerOneShotCntVector
          ├─ 每位 SynchronizerOneShot(rdClk) ──▶ statusStrobe(单拍事件)
          ├─ 每位 SynchronizerOneShotCnt(wrClk) ──▶ 各位事件计数
          └─ 计数总线经 FifoAsync(wrClk→rdClk) ──▶ cntOut(2D 计数值数组)
   │
   ▼  comb: statusStrobe 按 irqEnIn 屏蔽 → hitVector; 若 uOr(hitVector)='1' → irqOut
statusOut, cntOut, irqOut(全在 rdClk 域)
```

#### 4.3.3 源码精读

**RstSync**。实体声明里最值得注意的是 `RELEASE_DELAY_G` 的范围（[base/sync/rtl/RstSync.vhd:L25-L37](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/RstSync.vhd#L25-L37)）：

```vhdl
RELEASE_DELAY_G : integer range 3 to (2**24) := 3;  -- 异步→同步复位的撤销延迟
OUT_REG_RST_G   : boolean := true);                 -- 是否给最后一级也加异步复位
```

它把工作委派给一个特殊配置的 `Synchronizer`（[base/sync/rtl/RstSync.vhd:L48-L60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/RstSync.vhd#L48-L60)）：

```vhdl
Synchronizer_1 : entity surf.Synchronizer
   generic map (
      RST_POLARITY_G => IN_POLARITY_G,
      RST_ASYNC_G    => true,                          -- 链本身异步可复位
      STAGES_G       => RELEASE_DELAY_G-1,
      INIT_G         => slvAll(RELEASE_DELAY_G-1, OUT_POLARITY_G))  -- 复位值全填满
   port map (
      clk     => clk,
      rst     => asyncRst,
      dataIn  => not OUT_POLARITY_G,   -- 撤销时移入的是"无效值"
      dataOut => syncInt);
```

妙处在于 `INIT_G` 被填成全 `OUT_POLARITY_G`：复位有效期间整条链都是「复位有效值」，于是 `syncInt` 在复位时被牢牢按在有效电平上；一旦 `asyncRst` 撤销，`not OUT_POLARITY_G`（无效值）开始逐级移入，经过 `RELEASE_DELAY_G-1` 拍才把输出「释放」掉——这就把撤销对齐到了时钟。

最后一级 `OUT_REG` 进程做「异步置位 + 同步撤销」的最后一道（[base/sync/rtl/RstSync.vhd:L63-L70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/RstSync.vhd#L63-L70)）：

```vhdl
OUT_REG : process (asyncRst, clk) is
begin
   if (asyncRst = IN_POLARITY_G and OUT_REG_RST_G) then
      syncRst <= OUT_POLARITY_G after TPD_G;   -- 异步置位: 立即生效
   elsif (rising_edge(clk)) then
      syncRst <= syncInt after TPD_G;           -- 同步撤销: 只在时钟沿变
   end if;
end process OUT_REG;
```

注意这一级的注释（[L62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/RstSync.vhd#L62)）：它故意**不**加 `ASYNC_REG` 约束，目的是允许它被复制以扇出到多个复位网络、缓解时序——所以用 `OUT_REG_RST_G` 让你能按需关掉这级的异步复位。

**SyncStatusVector**。实体声明很长，因为它要同时描述「写侧（`wrClk` 域）输入」「读侧（`rdClk` 域）输出/计数/中断」两组端口（[base/sync/rtl/SyncStatusVector.vhd:L23-L92](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SyncStatusVector.vhd#L23-L92)）。几个关键泛型：`COMMON_CLK_G`（读写同源时整体退化为同步，省掉跨域开销）、`SYNC_STAGES_G`（电平同步级数，默认 3）、`CNT_WIDTH_G`（计数器位宽，默认 32）、`WIDTH_G`（状态向量位宽，默认 16）。`cntOut` 是一个二维 `SlVectorArray(WIDTH_G-1 downto 0, CNT_WIDTH_G-1 downto 0)`，文件头有大段注释说明它如何被外部重映射成普通的 SLV 数组（[base/sync/rtl/SyncStatusVector.vhd:L60-L70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SyncStatusVector.vhd#L60-L70)）。

架构体里先例化两个子件。**电平同步**用 `SynchronizerVector`，把 `BYPASS_SYNC_G` 绑成 `COMMON_CLK_G`（同源即直通）（[base/sync/rtl/SyncStatusVector.vhd:L112-L121](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SyncStatusVector.vhd#L112-L121)）。**事件计数**用 `SynchronizerOneShotCntVector`，它对每一位内部都做：

- 一个 `SynchronizerOneShot`（跑在 `rdClk`）产生该位的事件选通 `statusStrobe(i)`；
- 一个 `SynchronizerOneShotCnt`（跑在 `wrClk`）维护该位的事件计数；
- 当读写不同源时，再起一个小状态机轮询每个计数器，把 `{计数值, 索引}` 拼成一个字、经一个 `FifoAsync` 搬到 `rdClk` 侧重建出 `cntRdDomain`（见 [base/sync/rtl/SynchronizerOneShotCntVector.vhd:L197-L287](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShotCntVector.vhd#L197-L287)）。

这就是「多比特计数值」跨域的正确姿势——**不是逐位同步，而是打包成字走异步 FIFO**，呼应 2.3 的第一条铁律。

最后，`SyncStatusVector` 自己的 `comb` 进程把事件选通按 `irqEnIn` 屏蔽成 `hitVector`，只要有一位命中就拉起 `irqOut`（[base/sync/rtl/SyncStatusVector.vhd:L149-L178](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SyncStatusVector.vhd#L149-L178)）：

```vhdl
for i in 0 to (WIDTH_G-1) loop
   if irqEnIn(i) = '1' then
      v.hitVector(i) := statusStrobe(i);   -- 只统计被使能的位
   end if;
end loop;
if uOr(r.hitVector) = '1' then
   v.irqOut := OUT_POLARITY_G;             -- 任一命中即中断
end if;
```

`seq` 进程依然是 u1-l5 的标准异步/同步复位二选一骨架（[L180-L188](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SyncStatusVector.vhd#L180-L188)）。可以看到，`SyncStatusVector` 是把本讲前面三个模块（`Synchronizer`/`SynchronizerOneShot`）和 [u2-l2](u2-l2-fifo-blocks.md) 的异步 FIFO 组合起来，解决一个真实而复杂的需求——这正是它作为「综合件」的价值。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，确认 `RstSync`「异步置位、同步撤销」的时序关系；并理解 `SyncStatusVector` 的事件计数如何跨域。

**操作步骤**：

1. 打开 `RstSync` 的 `OUT_REG` 进程（[base/sync/rtl/RstSync.vhd:L63-L70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/RstSync.vhd#L63-L70)）。假设 `IN_POLARITY_G='1'`、`OUT_POLARITY_G='1'`：
   - 当 `asyncRst` 由 `'1'`→`'0'`（撤销）的瞬间，`syncRst` 会**立刻**变 `'0'` 吗？为什么？
   - 跟踪 `syncInt`：撤销后要等多少拍，`syncRst` 才真正变低？
2. 跑 `RstSync` 与 `SyncStatusVector` 的回归，确认行为：

   ```bash
   ./.venv/bin/python -m pytest -q tests/base/sync/test_RstSync.py tests/base/sync/test_SyncStatusVector.py
   ```

3. 在 [base/sync/rtl/SynchronizerOneShotCntVector.vhd:L251-L270](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/sync/rtl/SynchronizerOneShotCntVector.vhd#L251-L270) 处找到那个 `FifoAsync`，确认它是把「计数值 + 索引」打包成 `FIFO_WIDTH_C = CNT_WIDTH_G + bitSize(WIDTH_G-1)` 位的一个字再跨域的。

**需要观察的现象**：`RstSync` 在 `asyncRst` 撤销后，`syncRst` 不会立即跟随，而是延迟约 `RELEASE_DELAY_G` 拍后才释放——这正是「同步撤销」的可观察证据。

**预期结果**：两个测试用例全绿；你能口头回答步骤 1 的两个问题。运行结果**待本地验证**（依赖本机仿真环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么「复位的撤销」必须同步，而「复位的置位」却允许异步？

> **答**：置位（进入复位）要异步，是为了保证即使时钟还没跑（或正在跑但相位未知），电路也能被强制到一个已知状态，这是可靠性的底线。撤销（退出复位）要同步，是为了让域内所有触发器在**同一拍**同时「醒来」，否则有的寄存器先松开复位、有的还压在复位，下一个时钟沿就会采到不一致的状态机初值，行为不可预期。

**练习 2**：`SyncStatusVector` 里的计数值跨域，为什么用 `FifoAsync`，而不是像 `statusOut` 那样用 `SynchronizerVector`？

> **答**：因为计数器是一个**多位数值**，它的各比特必须「同时」到达目的域才算同一个数。`SynchronizerVector` 逐位独立同步，会撕裂数值。`FifoAsync` 用握手 + 存储把整个字原子地搬过去，才能保证读到的计数是一个一致的快照（这条理由和 4.1.5 的练习 2 完全一致）。

---

## 5. 综合实践

把本讲三个模块串成一个端到端的小任务：**为一个异步外设设计「复位 + 跨域触发 + 事件计数」三件套**。

设想你有：一个慢时钟域（`slowClk`，例如管理 MCU）产生复位 `extRst` 和触发脉冲 `extTrig`；一个快时钟域（`fastClk`，例如数据通路）需要消费它们。

**任务**：

1. **复位**：用 `RstSync` 把 `extRst` 转成 `fastClk` 域里「异步置位、同步撤销」的本地复位 `fastRst`（参考 4.3.3 的实例写法）。
2. **触发**：用 4.2.4 写好的 `SynchronizerOneShot` 实例，把 `extTrig` 变成 `fastClk` 域的单拍 `fastTrig`，复位置成 `fastRst`、`BYPASS_SYNC_G => false`。
3. **计数**：把 `fastTrig`（或外设回送的某个状态位）接进一个 `SyncStatusVector`（`COMMON_CLK_G => true` 即可，因为已在 `fastClk` 域），读它的 `cntOut` 得到「累计触发次数」，用 `irqEnIn` 使能某一位以产生中断。
4. 在 `cntRstIn` 上发一个脉冲清零计数器，观察 `cntOut` 归零后又随 `extTrig` 递增。

**验收要点**：

- `fastRst` 在 `extRst` 撤销后，延迟若干拍才释放（异步置位、同步撤销）。
- 无论 `extTrig` 持续多久，`fastTrig` 每次都只亮一拍。
- `cntOut` 与你手动给的触发次数一致；中断只在被使能的那一位翻转时出现。

> 本综合实践需要你自行搭建一个两时钟的 testbench（`slowClk` 与 `fastClk` 两套 `Clock`），可参考 [tests/base/sync/sync_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/base/sync/sync_test_utils.py) 的 `cycle`/`settle` 写法，但 `SyncStatusVector` 的多时钟版本要给 `wrClk` 和 `rdClk` 各起一个时钟。完整波形的逐拍时序**待本地验证**。

## 6. 本讲小结

- **跨域三大铁律**：单比特或已握手；目的域串 ≥2 级触发器且不许被工具优化；脉冲型信号要先变事件。
- `Synchronizer` 是原子积木：一条带 `ASYNC_REG`/`shreg_extract=no` 等属性的移位链，把亚稳态留给多级解析；`SynchronizerVector` 是它按位独立的向量版（只适合彼此无关的各比特）。
- `SynchronizerOneShot` 用「`RstSync` 整形 + `SynchronizerEdge` 边沿检测」把任意宽度的跨域脉冲压成目的域恰好一周期的脉冲，是「跨域触发」的标准做法。
- `RstSync` 实现「异步置位、同步撤销」，是每个时钟域生成本地复位的标配；`OUT_REG` 这级故意不加 `ASYNC_REG` 以便扇出。
- `SyncStatusVector` 把电平同步、事件计数（计数总线走 `FifoAsync` 原子跨域）、中断聚合三合一，展示了如何把本讲的几个原语组合成解决真实需求的综合件。
- 识别 CDC 边界的速记：看到一串 `attribute ... of <sig> ...` 围着某条信号，多半就是同步链。

## 7. 下一步学习建议

- 学完本讲后，自然的下一站是 [u2-l2 FIFO 构建块](u2-l2-fifo-blocks.md)：异步 FIFO 用 Gray 指针解决**多比特数据**的跨域问题，正好补上本讲反复强调「单比特用同步器、多比特用 FIFO」里的另一半。
- 想深入了解边沿/计数族的其他成员，可继续读 `base/sync/rtl/` 下的 `SynchronizerOneShotCnt.vhd`、`SyncClockFreq.vhd`（频率测量）、`SyncTrigRate.vhd`（触发速率测量），它们都建立在原讲讲的同一套 CDC 原语之上。
- 建议精读 `tests/base/sync/` 下与每个模块同名的 cocotb 测试，它们是理解模块「可观察行为」的最快途径，也为 [u9-1 cocotb 工具链](u9-l1-cocotb-toolchain.md) 的回归方法论做铺垫。
