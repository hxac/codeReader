# 综合实战：用积木组装一个完整小系统

> 本讲是整本手册的收官篇。前面六个单元我们一块一块地认识了 basic_verilog 里的「积木」——分频器、去抖器、边沿检测、同步器、FIFO、UART……这一讲要把它们拼成一台**能跑的小机器**：按一下按键，机器把一个计数字节塞进队列，再由串口一字一字地吐出去。拼装的过程中，你会真正理解三件事：模块怎么按数据流串接、异步按键怎么安全地进入系统时钟域、以及怎么写一个「自己判断对错」的端到端 testbench。

## 1. 本讲目标

学完本讲，你应当能够：

- 把 5~6 个此前学过的独立模块，按「生产者 → 缓冲 → 消费者」的数据流串接成一个完整系统，并正确处理模块间的握手时序。
- 在系统级解释并实现「异步按键 → 系统时钟域」的时钟域跨越（CDC），理解为什么这一步既需要同步器、又需要去抖。
- 编写一个端到端自检 testbench：用黄金模型驱动激励、解码串口波形、自动比对输出字节，让 bug 自己报错。

## 2. 前置知识

本讲不再重复各模块的内部细节，只用一句话唤醒你对此前讲义的记忆：

| 积木 | 来自 | 一句话回忆 |
|---|---|---|
| `clk_divider` | u2-l1 | 自由二进制计数器，`out[N]` 即 \(f_{\text{clk}}/2^{N+1}\)，常当「时钟树」/慢速采样源。 |
| `edge_detect` | u2-l2 | 用一级延迟寄存器比较得到 `rising`/`falling`/`both` 单拍脉冲。 |
| `delay` / `cdc_data` | u2-l3 / u3-l1 | `LENGTH=2` 即两级同步器；`cdc_data` 是它的封装，靠 `_SYNC_ATTR` 后缀统一加 `false_path`。 |
| `debounce_v2` | （本讲首登场） | 用 `clk_divider` 慢采样 + 整窗稳定判定的「低通」去抖。 |
| `fifo_single_clock_ram` | u4-l2 | 单时钟环形 FIFO，块 RAM 同步读（normal 模式，`r_data` 比请求晚一拍）。 |
| `uart_tx` | u5-l1 | 用 `BAUD_DIVISOR=CLK_HZ/BAUD` 当位节拍，10 位移位帧 `{1,data,0}`，`tx_busy` 提前置位/复位。 |
| testbench 方法 | u7-l1 | `$urandom` 随机激励 + 黄金模型自校验，让错误自动暴露。 |

如果你对其中某一块只剩模糊印象，建议先回看对应讲义再继续——本讲的重点是「拼」，而不是重新讲每一块。

## 3. 本讲源码地图

本讲会反复引用下面这些真实文件。注意：本讲要搭建的「小系统」**不是仓库里已有的某个文件**，而是我们用下面这些积木**新拼出来**的；所有由我们新写的代码都会明确标注为「示例代码」。

| 文件 | 在本讲的作用 |
|---|---|
| `clk_divider.sv` | 系统的「时钟树」源头；也是 `debounce_v2` 内部慢采样的实现基础。 |
| `debounce_v2.sv` | 按键去抖 + 异步输入的实用低通；它内部就例化了 `clk_divider` 与 `edge_detect`。 |
| `edge_detect.sv` | 把去抖后的电平转成「单拍写请求」。 |
| `fifo_single_clock_ram.sv` | 连接「慢速按键事件」与「慢速串口发送」的弹性缓冲队列。 |
| `uart_tx.sv` | 队列的消费者，把每个字节打成 UART 帧发出去。 |
| `main_tb.sv` | 仓库自带的 testbench 风格样板（时钟/复位/随机激励写法），是我们自检 testbench 的模仿对象。 |
| `example_projects/quartus_test_prj_template_v4/src/main.sv` | 仓库模板里**真实的**「异步输入 → 同步器 → 边沿检测」CDC 写法，是本讲 CDC 部分的一手证据。 |
| `cdc_data.sv` | 两级同步器的封装，CDC 章节的参照物。 |

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：**模块串接**、**CDC 系统级应用**、**端到端验证**。它们恰好对应「把积木拼起来 → 让异步世界安全接进来 → 证明拼对了」三步。

### 4.1 模块串接：从积木到数据通路

#### 4.1.1 概念说明

前六单元里，每个模块都是孤立的小零件。所谓「系统」，就是让数据像流水一样，从一个模块的输出「流」进下一个模块的输入。本讲要搭的通路只有一条主线：

> **异步按键 →（去抖）→ 去抖电平 →（边沿）→ 单拍写请求 →（写计数器值）→ FIFO →（读）→ UART →（移位）→ 串口线 `txd`**

这条主线天然分成三段，对应三个角色：

- **生产者（producer）**：按键事件。慢、稀疏、带抖动，每次产生一个单拍脉冲，顺手把一个计数字节写进队列。
- **缓冲（buffer）**：FIFO。因为「按键突然来一下」和「串口慢慢发一个字节」速度差好几个数量级，必须用队列把瞬时的写吸收下来。
- **消费者（consumer）**：UART 发送器。只要队列不空、自己又不忙，就取一个字节发出去，发完再取下一个。

这种「生产—缓冲—消费」结构是硬件设计里最通用的骨架之一，理解了它，你就能看懂绝大多数数据采集系统。

#### 4.1.2 核心流程

把主线画成数据流图（`=>` 表示数据/握手信号流向）：

```
            (异步, 带抖动)
   btn ───────► debounce_v2 ───► btn_db ───► edge_detect(rising) ───► btn_rise (单拍)
                                                                   │
                                                            (写请求 w_req)
                                                                   ▼
                              evt_cnt ──(w_data)──► fifo ──(r_data)──► uart_tx ──► txd
                                                     ▲                ▲
                                                  r_req            tx_start/tx_busy
                                                     └───── 消费者 drain FSM ─────┘
```

整条通路只跑在一个时钟域上（50 MHz 的 `clk`），因此串接时唯一要小心的是**握手时序**——尤其是两处「晚一拍」：

1. **FIFO 读出晚一拍**：块 RAM 是同步读，`r_req` 当拍发出，`r_data` 要到**下一拍**才有效。
2. **`tx_busy` 拉高晚一拍**：`tx_start` 当拍发出，`uart_tx` 在**下一拍**才把 `tx_busy` 置 1。

这两处「晚一拍」决定了消费者状态机不能「发完请求立刻查忙信号」，否则会读到旧值、误判空闲。具体怎么处理，见 4.1.3 和第 5 节。

#### 4.1.3 源码精读

**(a) 生产者的源头：`edge_detect` 的单拍脉冲**

写请求 `w_req` 来自 `edge_detect` 的 `rising`。它的本质是用一级延迟寄存器 `in_d` 暂存上一拍的输入，再和当前输入比较：

[edge_detect.sv:53-70](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv#L53-L70) —— 用 `in_d` 延迟一拍，`rising = in & ~in_d` 得到「本拍为高、上拍为低」的单拍上升脉冲。我们取 `REGISTER_OUTPUTS=1'b1`，让脉冲再寄存一拍，输出更干净、利于时序。

把这一拍脉冲直接当 `w_req`，就能做到「每按一次键，FIFO 写一字节」。

**(b) 缓冲：FIFO 的写与读**

写入侧很简单：`w_req` 一来，块 RAM 在同一时钟沿把 `w_data` 写进 `w_ptr` 指向的单元，`w_ptr` 随后回绕递增、`cnt` 加 1。见指针/计数更新逻辑：

[fifo_single_clock_ram.sv:124-163](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L124-L163) —— `unique case({w_req,r_req})` 仲裁同时读写；满空标志由 `cnt` 译码。

满空与非法访问报告是组合输出，**当拍**就给出：

[fifo_single_clock_ram.sv:165-171](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L165-L171) —— `empty=(cnt==0)`、`full=(cnt==DEPTH)`；`fail` 在「空读」或「满写」时拉高。我们的消费者必须先看 `empty` 再发 `r_req`，否则会触发 `fail`。

> ⚠️ 一个仓库里已知的小矛盾：`fifo_single_clock_ram.sv` 顶部参数写着 `FWFT_MODE = "TRUE"`，但 INFO 明确说「only normal mode is supported here」，且块 RAM 同步读本身就是 normal 模式。本讲按**实际行为（normal，读出晚一拍）**来设计消费者，与 u4-l2/u4-l3 的结论一致。

**(c) 消费者：UART 的位节拍与忙信号**

`uart_tx` 用一个自由下计数器 `tx_sample_cntr` 周期性地产生位节拍 `tx_do_sample`：

[uart_tx.sv:53-64](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv#L53-L64) —— `BAUD_DIVISOR=CLK_HZ/BAUD` 决定位宽；计数到 0 产生一个单拍 `tx_do_sample`。

发帧逻辑把 `{1'b1, tx_data, 1'b0}`（停止、8 数据 LSB 先、起始）装进 10 位移位器，在 `tx_do_sample` 时整体右移挤出 `txd`：

[uart_tx.sv:67-93](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/uart_tx.sv#L67-L93) —— 关键是 `tx_busy` 的**提前置位**（收到 `tx_start` 当拍就置 1）和**提前复位**（移位器还剩停止位时就清 0），使停止位期间可预装下一帧、实现背靠背连续发送。

对我们的消费者而言，握手要点是：`tx_start` 当拍给出去后，`tx_busy` **下一拍**才高；所以我们不能在「刚发完 `tx_start` 的那一拍」就去判断 `tx_busy`。

#### 4.1.4 代码实践

**实践目标**：在动手写代码前，先用纸笔把数据通路画清楚、把每一段信号的方向和拍数对齐——这是硬件设计最值钱的习惯。

**操作步骤**：

1. 照着 4.1.2 的数据流图，列出每个模块的**端口表**（参考各文件的例化模板）。
2. 标出两处「晚一拍」（FIFO 读、`tx_busy`），在图上用 `+1` 标注。
3. 写出生产者一段的最小逻辑（示例代码，非仓库原有）：

```systemverilog
// 示例代码：生产者（按键 → 写请求 + 写数据）
logic btn_db, btn_rise;
debounce_v2 #(.WIDTH(1), .SAMPLING_FACTOR(8)) db (
  .clk(clk), .nrst(nrst), .ena(1'b1), .in(btn), .out(btn_db));

edge_detect #(.WIDTH(1), .REGISTER_OUTPUTS(1'b1)) ed (
  .clk(clk), .anrst(nrst), .in(btn_db),
  .rising(btn_rise), .falling(), .both());

logic [7:0] evt_cnt;
always_ff @(posedge clk) begin
  if (~nrst)         evt_cnt <= '0;
  else if (btn_rise) evt_cnt <= evt_cnt + 8'd1;   // 每按一次 +1
end

assign w_req  = btn_rise;   // 单拍写请求
assign w_data = evt_cnt;    // 写入「按本次之前的计数」
```

**需要观察的现象**：每按一次键，`btn_rise` 应只出现**一个时钟周期**的高电平；`evt_cnt` 应在该拍之后 +1。

**预期结果**：连续按 3 次，FIFO 里依次写入 `0`、`1`、`2`（因为写入用的是非阻塞赋值前的旧值）。精确字节流以本地仿真为准。

#### 4.1.5 小练习与答案

**练习 1**：如果取消 `edge_detect`、直接把 `btn_db` 接到 `w_req`，会发生什么？
**答**：`btn_db` 在按下期间会**持续多个时钟周期**为高，FIFO 会在那段时间里**每个周期都写一次**，一次按键可能塞满整个 FIFO。`edge_detect` 的作用就是把「一段电平」压缩成「一个脉冲」，保证一次按键只写一次。

**练习 2**：`w_data = evt_cnt` 写的是「旧值」。若想第一次按下就写出 `1`，最少怎么改？
**答**：把赋值改成 `assign w_data = evt_cnt + 8'd1;`（组合加法），这样写入的是「本次按下对应的编号」。

---

### 4.2 时钟域跨越的系统级应用：按键异步域 → 系统域

#### 4.2.1 概念说明

真实按键是**完全异步**于你的 50 MHz 时钟的：人手按下按键的瞬间，时钟沿有可能恰好落在按键信号的翻转窗里。这正是 u3-l1 讲过的**亚稳态**温床——触发器可能采到一个非法电平，并随机地稳定到 0 或 1。

更糟的是，机械按键还会**抖动**：按下和松开的几毫秒内，触点会快速通断十几次。如果你直接对原始按键做 `edge_detect`，一次按压会被识别成十几个事件。

所以「按键进入系统域」其实要解决**两个**叠加的问题：

1. **CDC（亚稳态）**：让异步信号安全地穿过时钟域边界。
2. **去抖（debounce）**：把机械抖动压成一次干净的状态变化。

basic_verilog 给了两层武器，本讲把二者组合使用。

#### 4.2.2 核心流程

推荐的系统级按键处理是**两层串联**：

```
btn(异步+抖动) ──► [两级同步器 delay/cdc_data] ──► [debounce_v2 慢采样+整窗判定] ──► 干净电平
```

- **第一层：同步器**。一个 `LENGTH=2` 的 `delay`（即 `cdc_data`）把异步输入搬到系统域，用「第一级吃亚稳态、第二级等它稳定」换取指数级提升的 MTBF（详见 u3-l1）。
- **第二层：去抖**。`debounce_v2` 用一个很慢的采样时钟（由 `clk_divider` 分频得到）稀疏地看输入，并要求**整个采样窗内电平都稳定**才翻转输出——这既滤掉了抖动，又因为采样极稀疏，给亚稳态留出了充足的恢复时间。

两层并用是最稳妥的做法：同步器负责「物理安全」，去抖器负责「语义干净」。

#### 4.2.3 源码精读

**(a) 仓库模板里真实的 CDC 写法**

最权威的参考是 Quartus 工程模板 `main.sv` 处理板载拨码/按键的方式——它在 `edge_detect` **之前**先给异步输入套了一级 `delay` 两级同步器，并按约定命名为 `*_SYNC_ATTR`：

[example_projects/quartus_test_prj_template_v4/src/main.sv:119-142](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L119-L142) —— 对 `{SW,KEY}` 这 6 位异步输入先做 `delay #(.LENGTH(2)) sw_SYNC_ATTR`，再送进 `edge_detect` 取上升沿。`_SYNC_ATTR` 后缀让一条 `set_false_path` 通配约束就能豁免全部同步器（u3-l1/u7-l2 已讲）。

这就是本讲 CDC 的「标准答案」。

**(b) 同步器本身就是 `delay` 的封装**

[cdc_data.sv:43-55](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L43-L55) —— `cdc_data` 内部就是 `delay #(.LENGTH(2), .TYPE("CELLS"))`，实例名 `data_SYNC_ATTR`。把它例化在按键路径最前端，就完成了「物理层」CDC。

**(c) 去抖器：慢采样 + 整窗判定**

`debounce_v2` 内部自己就例化了 `clk_divider` 生成采样时钟，并用 `edge_detect` 取采样脉冲：

[debounce_v2.sv:54-78](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/debounce_v2.sv#L54-L78) —— `SAMPLING_FACTOR` 决定从 32 位计数器里取哪一位当采样脉冲 `do_sample`（取第 N 位即每个 \(2^{N+1}\) 拍采样一次）。采样极其稀疏，等于给亚稳态留了成千上万拍去恢复。

判定逻辑要求**整个采样窗内输入电平一致**才翻转输出：

[debounce_v2.sv:84-123](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/debounce_v2.sv#L84-L123) —— 用 `in_is_high`/`in_is_low` 两个标志记录窗口内是否见过高/低；到采样点时按 `{high,low}` 组合决定输出。窗口内既有高又有低（即抖动）时，输出保持不变（或按 `TREAT_UNSTABLE_AS_*` 参数处理），从而滤除抖动。

> 💡 一个值得思考的细节：`debounce_v2` 的 `in` 是在系统 `clk` 的 `always_ff` 里直接采的，**并没有**显式两级同步器。它对亚稳态的容忍来自「采样极稀疏 + 整窗判定」这个低成本的低通。对教学板上的低速按键，这通常够用；但在要求高可靠性的场合，仍建议像模板 `main.sv` 那样在前面再加一级 `delay/cdc_data`。这正是「教科书严谨」与「工程实用」之间的一次典型取舍。

#### 4.2.4 代码实践

**实践目标**：亲手体会「同步」与「去抖」分别在解决什么问题，并补上对应的时序约束。

**操作步骤**：

1. 阅读上面的 `main.sv:119-142`，确认模板对 `SW/KEY` 是「先 `delay` 同步、再 `edge_detect`」的顺序。
2. 为本讲系统的按键同步器写一条 Vivado `false_path` 约束（与 u3-l1/u7-l2 一致）。假设同步器实例名为 `btn_SYNC_ATTR`：

```tcl
# 示例代码：豁免按键同步器的第一级（亚稳态发生地）
set_false_path -to [get_cells -hier -filter {NAME =~ *btn_SYNC_ATTR/data_reg[1]*}]
```

3. 思考：如果把这条约束删掉、又用 500 MHz 过约束（见 u7-l2），综合工具会报什么？

**需要观察的现象**：加上约束后，时序报告里这条 CDC 路径应从关键路径列表中**消失**；不加则会被当成普通同步路径报「建立时间违例」（伪违例）。

**预期结果**：约束生效后该路径不再出现在 setup 违例列表中。具体报告条数待本地综合验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `false_path` 要指向同步器的**第一级** `data_reg[1]`，而不是第二级？
**答**：第一级寄存器正是亚稳态发生的地方（它直接采异步输入），这条到达它的路径注定无法满足建立/保持时间，必须豁免。第二级采的是第一级的输出（已是本域信号），应保留正常分析，所以不能豁免它。（详见 u3-l1、u7-l2。）

**练习 2**：`debounce_v2` 的 `SAMPLING_FACTOR` 调大（如从 8 改到 16）对去抖效果和响应延迟各有什么影响？
**答**：采样窗变成原来的 2 倍（\(2^{17}\) vs \(2^{16}\) 拍），能滤除更长的抖动、亚稳态恢复时间也更充裕；代价是输出对按键变化的**响应延迟**也翻倍。这是一个「鲁棒性 vs 灵敏度」的权衡。

---

### 4.3 端到端验证：自检 testbench

#### 4.3.1 概念说明

把系统拼好之后，怎么知道它**真的对**？盯着波形用眼睛数 0/1 是不可持续的——u7-l1 的核心理念就是「让 bug 自己报错」。对这条「按键 → FIFO → 串口」通路，端到端验证要做三件事：

1. **造一个会抖动的按键**：在 testbench 里模拟真实按键的按下/抖动/保持/松开，而不是给一个干净的方波。
2. **维护一个黄金模型**：在 testbench 里用一个独立变量记录「应该写出哪些字节」，作为正确答案。
3. **解码 `txd` 并自动比对**：用一个「软件 UART 接收器」把 `txd` 上的波形重新还原成字节，与黄金模型逐一比对，不一致就 `$error`。

这样 testbench 一跑完，要么静默通过，要么自动报出「第 N 个字节错了，期望 X 收到 Y」——这才是能长期维护的验证。

#### 4.3.2 核心流程

自检 testbench 的执行流：

```
1. 产生时钟 clk、释放复位 nrst
2. 反复「模拟一次按键按压（带抖动）」：
     a. 在 btn 上制造短暂抖动 → 稳定高 → 保持若干采样窗 → 抖动 → 稳定低
     b. 黄金模型：expected_queue.push(本次应写入的字节)
3. 并行运行的「软件 UART 接收」进程：
     a. 等 txd 的下降沿（起始位）
     b. 等 1.5 个位周期到 D0 中点
     c. 每个位周期采 1 位，拼出 8 位字节（LSB 先）
     d. 从 expected_queue 弹出一个字节比对：不等则 $error
4. 全部发完且比对一致 → $display("PASS")；否则 success=0
```

其中第 3 步的「起始位下降沿对齐 → 等 1.5 位到中点 → 每位中点采样」正是仓库 `uart_rx` 的采样思路（u5-l1 已讲）：在每位中点采样能最大程度容忍波特率误差。

#### 4.3.3 源码精读

**(a) 仓库 testbench 的时钟/复位/激励写法**

`main_tb.sv` 是我们模仿的样板。它用 `initial` + `forever` 产生主时钟，用带 `#` 延时的 `initial` 序列描述复位，并用 `$urandom_range` 给异步时钟注入抖动：

[main_tb.sv:18-48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L18-L48) —— `clk200` 用 `forever #2.5` 翻转（20 ns 周期=模拟 50 MHz 量级）；`rst` 用 `initial` 延时序列产生单次复位，`assign nrst = ~rst` 转成低有效。这是我们 testbench 的骨架。

它还示范了如何用 `initial` 产生一次性激励脉冲：

[main_tb.sv:100-129](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L100-L129) —— 用 `seq_cntr` 计数器在一段时间内拉高 `id` 作为 DUT 输入。我们造按键激励时沿用这种「计数器 + 条件拉高」的思路。

> ⚠️ 注意：`main_tb.sv` 引用了一个仓库里**并不存在**的 `module_under_test`，且对 `edge_detect` 用了 `.nrst(...)` 端口名（实际模块端口是 `.anrst`）。所以 `main_tb.sv` 是一个**风格样板**而非可直接编译的整文件。我们只学它的写法，自己写正确、自包含的 testbench。

**(b) 自检的关键：黄金模型 + 自动比对**

u7-l1 总结了两套自检套路：「复制比对」和「计数守恒」。本讲用的是后者的一种变形——**序列守恒**：每一次合法按键（经去抖+边沿后）应该恰好对应 FIFO 的一次写入、串口的一次发送。因此「按键次数 == 写入字节数 == 收到的串口字节数」，且收到的字节序列应与黄金模型一致。任何不一致都是 bug。

#### 4.3.4 代码实践

**实践目标**：写出能解码 `txd` 并自动比对的 testbench 骨架。

**操作步骤**：下面是「软件 UART 接收」进程的示例代码（非仓库原有），把它放进你的 testbench：

```systemverilog
// 示例代码：testbench 里的 txd 解码 + 自动比对（黄金接收端）
localparam int BIT_CYC = CLK_HZ / BAUD;        // 一个位周期 = 多少个 clk
logic [7:0] exp_q[$];                            // 黄金模型期望字节队列
bit        success = 1;

initial begin
  automatic logic [7:0] rx_byte;
  automatic int b;
  forever begin
    // 1) 等起始位下降沿（txd 空闲为 1）
    @(negedge txd);
    // 2) 等 1.5 个位周期，到 D0 中点
    #(BIT_CYC * 1.5) ;
    // 3) 连续采 8 位，LSB 先
    rx_byte = 0;
    for (b=0; b<8; b=b+1) begin
      rx_byte[b] = txd;
      #(BIT_CYC) ;                              // 下一位中点
    end
    // 4) 比对
    if (exp_q.size()==0) begin
      $error("意外收到一个字节 0x%0x，队列已空", rx_byte); success = 0;
    end else if (rx_byte !== exp_q.pop_front()) begin
      $error("字节不匹配：期望 0x%0x 收到 0x%0x", exp_q.pop_front(), rx_byte);
      success = 0;
    end
  end
end
```

**需要观察的现象**：每次模拟按键后，`exp_q` 应被 push 一个期望字节；`txd` 上应出现一帧 `0 起始 + 8 数据 + 1 停止`；解码进程应在每帧结束后打印或静默比对。

**预期结果**：若系统正确，全部发送完成后 `success` 保持 1，可打印 `PASS`。任何握手时序错误（如 FIFO 读出没对齐、`tx_busy` 误判）都会表现为字节丢失或错位，被 `$error` 抓住。精确波形待本地仿真验证。

> 注：上面用 `#(BIT_CYC)` 表达延时，需配合 testbench 顶部的 `` `timescale ``（如 `` `timescale 1ns/1ps ``），并把 `BIT_CYC` 换算成对应物理时间，或直接用 `repeat(BIT_CYC) @(posedge clk);` 计数 clk 周期，避免 timescale 依赖。

#### 4.3.5 小练习与答案

**练习 1**：为什么要「等 1.5 个位周期」而不是「1 个」再到第一位中点？
**答**：起始位下降沿发生在位的**起点**；数据位 D0 的中点距离起点是「半个起始位 + 一个完整 D0 位」= 1.5 个位周期。在中点采样能容忍收发波特率的微小偏差，这正是 `uart_rx` 的做法（u5-l1）。

**练习 2**：如何「故意改坏」黄金模型，以证明你的检查器本身是有效的？（u7-l1 的要求）
**答**：临时把 `exp_q.push(...)` 里 push 的期望值改成「错误值」（如全部 push `8'hFF`），重跑仿真。如果检查器**没有**报错，说明它根本没在工作（ bug 会被漏过）；只有它如期 `$error`，才证明检查器有效。验证完记得改回来。

---

## 5. 综合实践：搭建完整的「按键 → FIFO → 串口」小系统

这一节把前面三节串起来，给你一个**可动手**的端到端任务。

### 5.1 系统总览

- **主时钟**：50 MHz `clk`（testbench 里用 `` `timescale 1ns/1ps `` + `forever #10` 模拟，周期 20 ns）。
- **波特率**：115200，故 `BAUD_DIVISOR = 50_000_000/115200 ≈ 434`，一位约 434 个 `clk`，一帧 10 位约 4340 个 `clk`。
- **去抖采样**：为缩短仿真时间，`SAMPLING_FACTOR` 取 8（采样窗 \(2^9=512\) 拍 ≈ 10 µs），而不是 16。
- **FIFO**：`DEPTH=8`、`DATA_W=8`（每个事件一个字节）。

### 5.2 顶层模块骨架（示例代码）

下面给出完整的顶层骨架。**这是本讲新增的示例代码，不是仓库原有文件**，你需要新建一个 `capstone.sv`：

```systemverilog
// 示例代码：capstone.sv —— 按键事件经 FIFO 由串口上报（本讲新建）
module capstone #( parameter
  CLK_HZ = 50_000_000,
  BAUD   = 115200
)(
  input  logic clk,     // 50 MHz 系统时钟
  input  logic nrst,    // 同步复位，低有效
  input  logic btn,     // 异步按键（机械抖动）
  output logic txd      // UART 发送线
);

  // ============ 生产者：按键 → 去抖 → 边沿 → 写请求 ============
  logic btn_sync;       // 可选：在 db 前再加一级 delay/cdc_data，命名 btn_SYNC_ATTR

  logic btn_db;
  debounce_v2 #(.WIDTH(1), .SAMPLING_FACTOR(8)) db (
    .clk(clk), .nrst(nrst), .ena(1'b1), .in(btn), .out(btn_db));

  logic btn_rise;
  edge_detect #(.WIDTH(1), .REGISTER_OUTPUTS(1'b1)) ed (
    .clk(clk), .anrst(nrst), .in(btn_db),
    .rising(btn_rise), .falling(), .both());

  logic [7:0] evt_cnt;
  always_ff @(posedge clk) begin
    if (~nrst)         evt_cnt <= '0;
    else if (btn_rise) evt_cnt <= evt_cnt + 8'd1;
  end

  // ============ 缓冲：单时钟 FIFO（normal 模式，读出晚一拍）============
  logic        w_req,  r_req;
  logic [7:0]  w_data, r_data;
  logic        empty,  full,  fail;

  assign w_req  = btn_rise;
  assign w_data = evt_cnt;

  fifo_single_clock_ram #(.DEPTH(8), .DATA_W(8)) ff (
    .clk(clk), .nrst(nrst),
    .w_req(w_req), .w_data(w_data),
    .r_req(r_req), .r_data(r_data),
    .cnt(), .empty(empty), .full(full), .fail(fail));

  // ============ 消费者：FIFO → UART ============
  // drain FSM 必须避开两处「晚一拍」：
  //   ① FIFO 读：r_req 当拍发，r_data 下一拍才有效；
  //   ② tx_busy：tx_start 当拍发，tx_busy 下一拍才高。
  logic        tx_start, tx_busy;
  logic [7:0]  tx_data;
  enum {IDLE, POP, HOLD, SEND, WAIT} st;

  always_ff @(posedge clk) begin
    if (~nrst) begin
      st <= IDLE; r_req <= 1'b0; tx_start <= 1'b0; tx_data <= '0;
    end else begin
      r_req <= 1'b0; tx_start <= 1'b0;            // 默认单拍脉冲
      case (st)
        IDLE: if (~empty && ~tx_busy) begin       // 队列非空且 UART 空闲
                r_req <= 1'b1;                    // 发读请求（数据下下拍有效，见下）
                st    <= POP;
              end
        POP : st <= HOLD;                          // 本拍 r_req=1，RAM 锁存读出
        HOLD: begin                                // 本拍 r_data 已有效
                tx_data  <= r_data;
                tx_start <= 1'b1;                  // 下拍 tx_start=1
                st       <= SEND;
              end
        SEND: if ( tx_busy) st <= WAIT;            // 等 tx_busy 真正拉高（避开 1 拍空洞）
        WAIT: if (~tx_busy) st <= IDLE;            // 等整帧发完
      endcase
    end
  end

  uart_tx #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) ut (
    .clk(clk), .nrst(nrst),
    .tx_data(tx_data), .tx_start(tx_start),
    .tx_busy(tx_busy), .txd(txd));
endmodule
```

**理解 drain FSM 的拍级对齐**（这是本实践的核心难点，建议对照波形逐拍确认）：

| 拍（相对） | 状态 | `r_req` | `r_data` | `tx_start` | `tx_busy` | 说明 |
|---|---|---|---|---|---|---|
| T | IDLE | 0→1 | 旧 | 0 | 0 | 决定取一个字节 |
| T+1 | POP | 1 | 旧 | 0 | 0 | RAM 在此拍锁存读出 |
| T+2 | HOLD | 0 | **有效** | 0→1 | 0 | 取走 `r_data`，发起 `tx_start` |
| T+3 | SEND | 0 | 保持 | 1→0 | 0 | `tx_busy` 还没高，原地等 |
| T+4 | SEND/WAIT | 0 | — | 0 | **1** | `tx_busy` 拉高，进入等待 |
| … | WAIT | 0 | — | 0 | 1 | 整帧移位中（≈4340 拍） |
| 末 | WAIT→IDLE | 0 | — | 0 | 0→ | 发完，回 IDLE 取下一字节 |

> 这张表是「待本地验证」的：实际拍数取决于你例化的参数，但状态间的先后关系（先读、再等数据、再发起、再等忙信号升起、最后等忙信号落下）是确定的。请在波形里按这张表逐拍核对。

### 5.3 testbench 任务

1. **产生时钟与复位**：模仿 `main_tb.sv:18-48` 的 `initial`/`forever` 写法。
2. **模拟 3 次按键**：每次按下时，先在 `btn` 上制造 ~10 个随机的短抖动（用 `#(随机小延时) btn=随机;`），稳定高保持 ≥ 2 个采样窗，再抖动着松开。每模拟完一次合法按压，向黄金队列 `exp_q.push(期望字节)`。
3. **解码 `txd` 并比对**：用 4.3.4 的「软件 UART 接收」进程，把收到的字节与 `exp_q` 逐一比对。
4. **收尾**：等待足够长时间（让 UART 把队列发空），检查 `exp_q.size()==0` 且无 `$error`，打印 `PASS`/`FAIL`。

### 5.4 编译运行

仿照仓库的 iverilog 脚本（`-g2012` 是 SystemVerilog 必需），把你新建的文件连同依赖一起编译：

```bash
# 示例命令：参考 scripts/iverilog_compile.bat 改写为 Linux/shell 版
iverilog -Wall -g2012 -s capstone_tb \
  -o capstone.vvp \
  capstone_tb.sv capstone.sv \
  clk_divider.sv debounce_v2.sv edge_detect.sv \
  fifo_single_clock_ram.sv true_dual_port_write_first_2_clock_ram.sv \
  uart_tx.sv clogb2.svh
vvp capstone.vvp          # 产出 .vcd 波形
```

> 说明：`fifo_single_clock_ram` 内部 `include "clogb2.svh"`，因此命令行要把 `clogb2.svh` 所在目录加入 include 路径（`-I.`），或随之上榜；它还例化 `true_dual_port_write_first_2_clock_ram`，所以也要一起编译。具体路径与脚本以本地仓库为准。

### 5.5 预期结果与检查清单

- [ ] 每次按键（带抖动）后，`btn_rise` 恰好出现**一个**单拍脉冲，`evt_cnt` +1。
- [ ] FIFO 的 `cnt` 每次按键后 +1，被 UART 取走后 -1；`fail` 全程为 0（无空读/满写）。
- [ ] `txd` 上每次按键对应**一帧**完整 UART 波形（起始 0 + 8 数据 + 停止 1）。
- [ ] 解码出的字节序列与黄金模型一致，最终打印 `PASS`。
- [ ] 把 `SAMPLING_FACTOR` 调大到 16，响应延迟应明显变长但仍功能正确（验证 4.2 的权衡）。

> 上述波形观察点需在本地仿真器（iverilog+GTKWave 或 ModelSim）中确认；本讲不假设你已经跑过，请如实记录你看到的现象。

## 6. 本讲小结

- 「系统」= 把模块按**生产者 → 缓冲 → 消费者**的数据流串接；串接的关键不是接线，而是**握手时序**——本讲里有两处「晚一拍」（FIFO 同步读、`tx_busy` 拉高），消费者状态机必须显式避开。
- 异步按键进入系统域要同时解决 **CDC（亚稳态）** 和 **去抖（机械抖动）**：用 `delay/cdc_data` 两级同步器负责物理安全，用 `debounce_v2` 的稀疏采样 + 整窗判定负责语义干净；仓库模板 `main.sv` 的 `*_SYNC_ATTR` 写法是标准答案。
- FIFO 在这里扮演**弹性缓冲**：它吸收「瞬时按键写入」与「慢速串口发送」之间的速率差，单时钟域内的「跨速率」正是 FIFO 最经典的用途。
- 端到端验证的精髓是**让 testbench 自己判对错**：造抖动激励 + 维护黄金模型 + 软件解码 `txd` 自动比对，bug 会自己 `$error` 出来。
- 本讲还示范了一个可复用的验证习惯：**先画数据流图、再标「晚一拍」、最后写 FSM**——这套流程适用于任何多模块系统。
- 全程以仓库真实代码为依据（`edge_detect`、`debounce_v2`、`fifo_single_clock_ram`、`uart_tx`、模板 `main.sv`、`cdc_data`），所有新写代码均明确标注为「示例代码」。

## 7. 下一步学习建议

到这里，你已经能独立读懂 basic_verilog 的每个模块，并把它们组装成系统。接下来可以：

1. **给本讲系统「加料」**：把 `cdc_data` 同步器真正加到按键路径最前端（实例名 `btn_SYNC_ATTR`），并补一条 Vivado/Quartus `false_path` 约束，对比加约束前后的时序报告（呼应 u7-l2）。
2. **换成双时钟域版本**：让 UART 跑在另一个时钟上，把单时钟 FIFO 换成跨时钟 FIFO，重新体会 CDC 的难度跃升（结合 u3-l1/u3-l2）。
3. **补全收方向**：用 `uart_rx` 做一个「串口命令 → 点 LED」的反向通路，与本章的发送通路合成一个完整的命令交互系统。
4. **跑一次真实基准**：参考 u7-l3，把你的 `capstone.sv` 放进 `example_projects` 或 `benchmark_projects` 模板，在 Quartus/Vivado 里综合，用 `get_fmax` 脚本读出 Fmax，看看这棵「积木系统」能跑多快。
5. **回读源码**：带着本讲的系统视角，重读 `debounce_v2.sv`（它内部 `clk_divider`+`edge_detect` 的组合，本质就是一个微型「系统」）和模板 `main.sv`（一个更完整的上板系统），你会发现它们不再陌生。

祝你把积木越拼越大。
