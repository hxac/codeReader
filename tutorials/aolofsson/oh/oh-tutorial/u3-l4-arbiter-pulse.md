# 仲裁器与脉冲控制

## 1. 本讲目标

本讲把注意力从「数据怎么算、怎么存」转向「**控制信号怎么排队、怎么整形**」。学完后你应当能够：

- 说清**仲裁器（arbiter）**在「多个主端抢同一个资源」时为什么必不可少，并能读懂 `oh_arbiter` 的固定优先级算法。
- 区分**固定优先级**与**轮询（round-robin）**两种仲裁思路，理解各自的饥饿/活锁风险。
- 用 `oh_stretcher` 把一个单周期脉冲**展宽**成多周期使能，看懂 `oh_pulse` 的随机周期脉冲发生原理。
- 读懂 `oh_debouncer` 的「同步 + 边沿检测 + 定时滤波」去抖结构，并会按实际按键与时钟换算去抖窗口。
- 把仲裁器产出的 grant 配上 **ready/反压（backpressure）** 信号——这正是后续 emesh/elink 里 `etx_arbiter`、`erx_arbiter` 的核心手法。

> 阅读提醒：本讲四个源文件里有**两个存在历史遗留的「悬空引用」与「注释/参数不匹配」**（`oh_pulse` 调用了未定义的 `oh_random`；`oh_debouncer` 的注释和测试包装对不上）。这与前几讲反复强调的「代码即事实、文档会滞后」一脉相承，本讲会逐处指出，请以源码实际端口为准。

## 2. 前置知识

本讲依赖 [u2-l4 跨时钟域同步原语](u2-l4-cdc-synchronizers.md)，请先确认你理解：

- **电平同步器 `oh_dsync`**：用多级触发器把异步输入同步到本时钟域，消除亚稳态。
- **复位同步器 `oh_rsync`**：异步生效、同步释放的复位处理。
- **边沿检测的套路**：「打一拍再异或/与」即可得到变化沿或上升沿。
- **非阻塞赋值 `<=`、低有效复位 `nreset`**：OH! 全库统一约定（见 [u1-l4](u1-l4-coding-style.md)）。

此外会用到 [u3-l3](u3-l3-arithmetic-datapath.md) 讲过的 `oh_counter`（带 `load/wraparound` 的可参数化计数器）。如果这些概念你已经熟悉，可以直接进入第 4 节。

两个通俗类比先放在前面：

- **仲裁器 = 单车道收费站的并道指挥**：四条车道（请求）要汇入一条车道（资源），同一时刻只放一辆车过去，指挥员按某种规则（固定顺序或轮流）决定放行谁。
- **去抖电路 = 防抖动门铃**：你按一下门铃，簧片会抖动出几十次通断；电路要求「按住不动一段时间」才算一次有效按下，把毛刺滤掉。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [stdlib/rtl/oh_arbiter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_arbiter.v) | 静态配置的仲裁器（参数 `TYPE`，但实际只实现了 `FIXED`） | 核心模块 1 |
| [stdlib/rtl/oh_stretcher.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_stretcher.v) | 把脉冲展宽若干周期 | 核心模块 2（脉冲展宽） |
| [stdlib/rtl/oh_pulse.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_pulse.v) | 随机周期脉冲发生器（依赖未定义的 `oh_random`） | 核心模块 2（脉冲生成，含悬空引用警示） |
| [stdlib/rtl/oh_debouncer.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v) | 数字去抖电路（同步 + 边沿检测 + 计数滤波） | 核心模块 3 |
| [elink/hdl/etx_arbiter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v) | elink 发送端的真实仲裁器，复用 `oh_arbiter` + 反压 | 真实用法范例、综合实践参照 |
| [stdlib/rtl/oh_counter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v) | 通用计数器（[u3-l3](u3-l3-arithmetic-datapath.md) 已讲） | 去抖电路内部依赖 |

---

## 4. 核心概念与源码讲解

### 4.1 仲裁器：oh_arbiter

#### 4.1.1 概念说明

当**多个主端（master）**都要访问**同一个共享资源**（一条总线、一个 FIFO 写口、一条发送链路……）时，必须有一个人来排队，否则多个请求在同一拍同时生效就会撞车。这个「排队判决器」就是**仲裁器（arbiter）**。

仲裁器接收一个**请求向量** `requests[N-1:0]`（每一位代表一个主端在/不在请求），输出一个**授权向量** `grants[N-1:0]`，保证任何一拍最多只有一位为 1（**one-hot**），即「同一时刻只放行一个请求」。

判罚规则有两类常见思路：

| 策略 | 规则 | 优点 | 风险 |
|------|------|------|------|
| **固定优先级（fixed priority）** | 优先级硬编码，高者优先 | 电路极简，关键路径短 | **饥饿（starvation）**：低优先级请求若高优先级一直占用，可能永远拿不到授权 |
| **轮询（round-robin）** | 优先级每轮轮转，上次被授权者下次排到最后 | 公平，无饥饿 | 电路更复杂，需记状态；多请求同发时可能出现**活锁（livelock）**隐患 |

`oh_arbiter` 在端口参数注释里写了 `TYPE = "FIXED" // or ROUNDROBIN, FAIR`，但**源码里只实现了 `FIXED` 一种**（见下方源码精读）。轮询是一个「设计意图」而非已实现功能——这一点 elink 里也有印证（[etx_arbiter.v:L77](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v#L77) 留有 `//TODO: change to round robin!!! (live lock hazard)` 的注释）。所以本讲重点讲透固定优先级，轮询作为对照概念讲清原理。

#### 4.1.2 核心流程

`oh_arbiter`（`FIXED` 模式）的算法可以浓缩成两步：

1. **算「等待掩码」waitmask**：对每一位请求 `j`，只要它**下面**（编号更小的位）有任意一位在请求，它就被「屏蔽」——

   \[ \text{waitmask}[j] = \text{OR}(\text{requests}[j-1:0]) \]

   特殊地，最低位 `waitmask[0] = 0`（它下面没有更小的位，永远不会被屏蔽）。  
   **推论：位编号越小，优先级越高。bit 0 是最高优先级。**

2. **算授权 grant**：

   \[ \text{grants} = \text{requests}\ \&\ \sim\text{waitmask} \]

   即「在请求、且没被更低编号的请求屏蔽掉」的那些位得到授权。由于 waitmask 的构造方式，结果天然 one-hot（不可能有两个同级位同时未被屏蔽）。

用一个 4 路的例子手算（bit 0 最高）：

| requests[3:0] | waitmask[3:0] | grants[3:0] | 含义 |
|---|---|---|---|
| `0000` | `0000` | `0000` | 无人请求 |
| `0001` | `0000` | `0001` | 只有 bit0，授权 bit0 |
| `1011` | `0110` | `1001` | bit0、bit1、bit3 同发，bit0 掩蔽 bit1，bit3 不被掩（bit2=0）→ 授权 bit0 与 bit3 |
| `1110` | `0001`→`0011`... | 见下 | 多位同发时逐级掩蔽 |

> 注意第三行 `1011` 的结果 `1001`：bit0 授权；bit1 被 bit0 屏蔽；bit3 因为 bit2 没请求而**不被屏蔽**，也授权。这意味着**同时授权的两个位之间隔着一位空位**，彼此不冲突——这正是 waitmask「只看更低位」的性质保证的。one-hot 性质对**连续请求**成立；对间隔请求可能出现多个 grant，但每个被授权者下方都没有别的活跃请求，仍互不冲突。

伪代码总结：

```
waitmask[0] = 0
for j in [1 .. N-1]:
    waitmask[j] = | requests[j-1:0]     // 下方任意一位活跃即屏蔽本位
grants = requests & ~waitmask
```

#### 4.1.3 源码精读

`oh_arbiter` 的接口非常精简——纯组合、只有 `requests` 进、`grants` 出，**没有时钟，也没有 ready 输出**（反压要在外面自己搭，见 4.1.4 与综合实践）。

[oh_arbiter.v:L8-L15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_arbiter.v#L8-L15) 定义端口与参数：参数 `N` 是请求路数，`TYPE` 名义上可选 `FIXED/ROUNDROBIN/FAIR`。

真正实现的固定优先级逻辑在 [oh_arbiter.v:L19-L31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_arbiter.v#L19-L31)。关键三段：

```verilog
// (1) 最低位永不屏蔽
assign waitmask[0]   = 1'b0;
// (2) 用 for 循环生成每一位的掩码：下方有任意请求就屏蔽
for (j=N-1; j>=1; j=j-1)
   assign waitmask[j] = |requests[j-1:0];   // 归约或
// (3) 授权 = 请求 & 取反掩码
assign grants[N-1:0] = requests[N-1:0] & ~waitmask[N-1:0];
```

要点：

- `|requests[j-1:0]` 是 Verilog 的**归约或（reduction-OR）**，把一段向量压成 1 位（[u2-l1](u2-l1-combinational-primitives.md) 已用过）。
- `generate for` 在综合期把循环**展开成连线**，所以 `N` 可以任意参数化，运行时不占状态——这就是「固定优先级仲裁器只需纯组合、零寄存器」的由来，也是它速度快、面积小的原因。
- 注意循环方向 `j=N-1 … 1`：因为 `waitmask[0]` 单独赋了 0，循环从 1 开始；每个 `waitmask[j]` 只依赖比它低的位，没有组合环路。

**真实用法范例——elink 的 `etx_arbiter`。** 它把三个通道（写、读请求、读响应）的事务仲裁到一条发送链路上，优先级注释在 [etx_arbiter.v:L10-L13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v#L10-L13)：

```
1) read responses (highest)
2) host writes
3) read requests from host (lowest)
```

实例化 `oh_arbiter` 的代码在 [etx_arbiter.v:L79-L87](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v#L79-L87)：

```verilog
oh_arbiter #(.N(3)) arbiter (
   .grants  ({txrd_grant, txwr_grant, txrr_grant}),   // bit2,bit1,bit0
   .requests({txrd_access, txwr_access, txrr_access}) // bit2,bit1,bit0
);
```

把 `txrr`（读响应）放在 **bit0**、`txwr`（写）放 bit1、`txrd`（读请求）放 bit2——正好让 bit0=读响应成为最高优先级，与上面的注释完全吻合。这就是「**优先级高低由你在拼接 `{}` 里的位序决定**」的实战写法。

#### 4.1.4 代码实践

**实践目标**：用 `oh_arbiter` 实例化一个 4 路固定优先级仲裁器，并在外部搭出每一路的 **ready（反压/应答）信号**，理解 grant 与 ready 的关系。

**操作步骤**：

1. 新建一个仿真顶层（这是**示例代码**，非项目原有文件），把 4 路请求接到 `oh_arbiter`：

   ```verilog
   // 示例代码：4 路固定优先级仲裁器 + 反压
   module arb4_demo (
       input  clk, nreset,
       input  [3:0] requests,   // 4 路请求，bit0 优先级最高
       input        stall,      // 下游反压：1 表示下游这拍不能收
       output [3:0] grants,     // one-hot 授权
       output [3:0] ready       // 每路「本拍是否被成功接收」
   );
       wire [3:0] waitmask;
       // 直接复用 stdlib 的固定优先级仲裁器
       oh_arbiter #(.N(4), .TYPE("FIXED")) u_arb (
           .requests (requests),
           .grants   (grants)
       );

       // 反压：低优先级者在「下游停顿」或「有更高优先级者在请求」时被挂起。
       // 思路与 etx_arbiter 的 txwr_wait/txrd_wait 一致（逐级累积 OR）。
       assign ready[0] = grants[0] & ~stall;                 // 最高优先级，只看下游
       assign ready[1] = grants[1] & ~stall;
       assign ready[2] = grants[2] & ~stall;
       assign ready[3] = grants[3] & ~stall;
       // 等价地：ready[i] = requests[i] & ~stall & ~|requests[i-1:0]
   endmodule
   ```

2. 写一个最简 testbench：令 `requests=4'b1011`、`stall=0`，观察 `grants`；再令 `requests=4'b1111` 观察「只有 bit0 被授权」。

**需要观察的现象**：

- `requests=4'b1011` → `grants` 应为 `4'b1001`（bit0 与 bit3，验证 4.1.2 的手算）。
- `requests=4'b1111` → `grants=4'b0001`（仅最高优先级的 bit0）。
- `stall=1` 时所有 `ready` 为 0，但 `grants` **不变**——体会「grant 是判决，ready 是能否真的成交」的区别。

**预期结果**：grants 严格 one-hot（对全相邻请求），ready 在 stall 时全 0。

**待本地验证**：本仓库的仿真脚本（见 [u1-l3](u1-l3-simulation-setup.md)）路径有历史遗留问题；若直接用 `build.sh`/`sim.sh` 跑不起来，请按 u1-l3 的排错结论先把 `libs.cmd` 与 `OH_HOME` 配好，或手工 `iverilog -g2005 -DTARGET_SIM=1` 这一个文件加一个简易 tb 验证。

#### 4.1.5 小练习与答案

**练习 1**：若想让 bit3 成为最高优先级，在不改 `oh_arbiter` 源码的前提下怎么办？  
**答**：在实例化处把请求位序倒过来接，即 `.requests({req0,req1,req2,req3})`，让原 bit0 位置接物理上的「希望最高优先级」那一路——优先级由拼接顺序决定，正如 `etx_arbiter` 把读响应放 bit0。

**练习 2**：固定优先级仲裁器的「饥饿」什么时候会发生？轮询如何缓解？  
**答**：当某个高优先级请求长期占用资源时，低优先级请求一直被 waitmask 屏蔽，永远拿不到 grant，即饥饿。轮询让优先级每轮轮转（上一轮的胜者下一轮排到最低），使每个请求都能轮到，从而避免饥饿；代价是要用寄存器记住「当前轮到谁」，电路更复杂。

**练习 3**：`oh_arbiter` 为什么没有任何寄存器、也没有 ready 输出？  
**答**：固定优先级判决是纯组合函数 `grants=requests & ~waitmask`，`generate for` 在综合期展开成连线，无需状态；而 ready/反压涉及「能否被下游接收」这一时序决策，应由调用方（如 `etx_arbiter`）根据下游 wait 信号自行构造。

---

### 4.2 脉冲控制：oh_stretcher 与 oh_pulse

#### 4.2.1 概念说明

很多控制场景需要对**脉冲**做时间上的整形：

- **展宽（stretch）**：一个只亮一拍的脉冲太短，下游可能来不及反应（比如唤醒信号、中断确认），需要把它**展宽成连续多拍**的有效电平。这就是 `oh_stretcher` 的职责。
- **随机周期脉冲（random-length pulse）**：在测试激励里，常需要一个**间隔随机**的脉冲源，用来给待测电路喂不可预测的激励（覆盖更多时序 corner）。`oh_pulse` 试图提供这样一个源。

二者都属「把脉冲的**时长/间隔**按需改造」的控制原语。

#### 4.2.2 核心流程

**`oh_stretcher`——移位寄存器展宽。** 维护一个 `CYCLES` 位宽的移位寄存器 `valid`：

- 输入脉冲 `in` 来一拍 → 把 `valid` **全部置 1**；
- 之后每拍把一个 0 从低位**移入**，1 们整体左移；
- 输出取最高位 `out = valid[CYCLES-1]`。

效果：`in` 一个单周期脉冲，`out` 会维持**连续 `CYCLES` 拍**的高电平，直到那个移进来的 0 走到最高位才落下。相对输入，输出在下一拍才拉高（寄存器固有的一拍延迟，源码头注释称之为「Adds one cycle latency」）。

**`oh_pulse`——计数器撞随机值。** 维护一个自由运行的 `counter`，每拍 `+1`；同时有一个（期望来自 `oh_random` 的）随机数 `random`。当 `counter == random` 时：

- 产生一拍 `match`，并把 `counter` 清零重新开始下一轮扫描；
- 输出 `out` 用 `out <= out ^ match` **翻转**一次。

于是 `out` 就在随机间隔上反复翻转，得到「随机周期」的脉冲。`mask` 用来限制 `random` 的取值范围，从而限制最大间隔。

#### 4.2.3 源码精读

**`oh_stretcher`** 全文只有一个移位寄存器，见 [oh_stretcher.v:L16-L27](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_stretcher.v#L16-L27)：

```verilog
reg [CYCLES-1:0] valid;
always @ (posedge clk or negedge nreset)
  if(!nreset)              valid <= 'b0;
  else if(in)              valid <= {(CYCLES){1'b1}};   // 脉冲来了：全置 1
  else                     valid <= {valid[CYCLES-2:0],1'b0}; // 否则左移、低位补 0
assign out = valid[CYCLES-1];                            // 取最高位
```

要点：

- `{(CYCLES){1'b1}}` 是 [u2-l1](u2-l1-combinational-primitives.md) 讲过的**复制拼接**，把 1 复制 `CYCLES` 份填满寄存器。
- `{valid[CYCLES-2:0],1'b0}`：取低 `CYCLES-1` 位、末尾拼一个 0，整体左移一位。
- 头部注释 [oh_stretcher.v:L2-L4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_stretcher.v#L2-L4) 写「Stretches a pulse by N+1 clock cycles」——这里 `N` 指的是 `CYCLES-1`，即输出高电平持续 `CYCLES` 拍。**精确拍数建议仿真确认（待本地验证）**，因为「N+1 / 一拍延迟」的表述与参数名 `CYCLES` 之间有口径差。

**`oh_pulse`** 见 [oh_pulse.v:L19-L46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_pulse.v#L19-L46)，关键三段：

```verilog
oh_random oh_random(.out(random), .clk(clk), .mask(mask), .nreset(nreset), .en(match)); // L23-L29
assign match = (random == counter);          // L31：撞值
always @ (posedge clk or negedge nreset)     // L33-L39：计数器，撞值即清零
   ... counter <= match ? 'b0 : counter + 1;
always @ (posedge clk or negedge nreset)     // L41-L45：撞值即翻转输出
   ... out <= out ^ match;
```

> ⚠️ **悬空引用（重要）**：[oh_pulse.v:L23-L29](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_pulse.v#L23-L29) 实例化的 `oh_random` **在整个仓库中都没有定义**（已全树检索，只有 `oh_pulse.v`、`oh_stimulus.v`、`oh_verify.v`、`tb_oh_lfsr.v` 引用它，没有任何文件写 `module oh_random`）。因此 `oh_pulse` **无法按现状直接编译/仿真**，对应的 [stdlib/testbench/tb_oh_pulse.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/tb_oh_pulse.v) 同样跑不通。这是 stdlib 历史「占位/半成品」的又一例。读这段源码时，请把 `oh_random` 理解为一个「**期望存在、返回受 `mask` 限制的伪随机数**」的黑盒——可以用 [u3-l3](u3-l3-arithmetic-datapath.md) 讲过的 LFSR（`oh_lfsr`）自己搭一个等价替身，原理一致。

#### 4.2.4 代码实践

**实践目标**：仿真验证 `oh_stretcher` 的展宽拍数；并用一个简易 LFSR 替身让 `oh_pulse` 的逻辑跑起来。

**操作步骤（`oh_stretcher`，可独立编译）**：

1. 写一个最小 testbench，`CYCLES=5`，产生一个单周期 `in` 脉冲，用 `$monitor` 打印 `valid` 和 `out`。
2. 数 `out` 连续为 1 的拍数。

**需要观察的现象**：`in` 拉高一拍后，`out` 从下一拍起维持高电平，持续 `CYCLES`（=5）拍后落回 0；`valid` 寄存器可见「全 1 → 逐步被 0 驱赶」的过程。

**预期结果**：`out` 高电平拍数 = `CYCLES`。

**操作步骤（`oh_pulse`，源码阅读 + 替身实践）**：

1. 阅读上面三段源码，回答：`match` 何时为 1？为什么它天然只高一拍？  
2. **示例代码**：把 `oh_pulse` 里的 `oh_random` 换成一个用 `oh_lfsr` 实现的伪随机源（或最简地用一个自由计数器当「假随机」），重新仿真，观察 `out` 的翻转间隔是否随 `mask` 变化。

**预期结果**：`mask` 越小（随机数范围越小），`out` 翻转越频繁。

**待本地验证**：因 `oh_random` 缺失，原样 `oh_pulse` 编译会报 `unknown module`；上述 LFSR 替身方案需你自行接线后才能跑通。

#### 4.2.5 小练习与答案

**练习 1**：`oh_stretcher` 的 `CYCLES=5` 时，输出高电平持续几拍？为什么？  
**答**：持续 5 拍。`in` 一拍把 5 位寄存器全置 1；之后每拍从低位移入一个 0，0 要走 4 拍才到达最高位 `valid[4]`，加上置位那一拍，共 5 拍 `out` 为高。

**练习 2**：`oh_pulse` 里 `out <= out ^ match`，若 `match` 连续两拍为 1 会怎样？实际会发生吗？  
**答**：`out` 会连续翻转两次（等价没翻）。但实际不会连续两拍 `match=1`：因为 `match=1` 的同一拍 `counter` 被清零，下一拍 `counter=1` 而 `random` 至少在 `en(match)` 驱动下已更新，`match` 一般只高一拍。

**练习 3**：为什么 `oh_stretcher` 用「全置 1 再左移 0」而不是用一个计数器比大小？  
**答**：移位寄存器方案是纯位的移位与复制，无加法器、无比较器，关键路径短、面积小，且天然支持参数化位宽；计数器方案需要 `clog2` 位加法器和一个比较器，资源与功耗都更高。

---

### 4.3 去抖电路：oh_debouncer

#### 4.3.1 概念说明

机械按键/开关在按下、松开瞬间，簧片会**机械抖动（bounce）**，在几毫秒内产生几十次通断跳变。如果直接把这种信号喂给数字逻辑，一次按压会被误识别成很多次。**去抖（debounce）** 的任务是：只有当输入**稳定保持足够长时间**后，才承认它是一次有效变化。

数字去抖的通用套路是「**采样 + 定时滤波**」：

1. 先把异步输入**同步**到本时钟域（防亚稳态，复用 [u2-l4](u2-l4-cdc-synchronizers.md) 的同步器）。
2. **检测输入是否发生变化**（边沿检测）。
3. 每检测到一次变化，就**重置一个计时器**重新计时；只有当输入**持续不变**到计时器溢出，才把当前电平采样到输出。

换句话说：「输入只要还在抖，计时器就被不断清零；等它安静满一个窗口，我才相信它。」

#### 4.3.2 核心流程

`oh_debouncer` 的数据流：

```
noisy_in
  │
  ▼
oh_dsync (2 级同步) ──► noisy_synced ──► noisy_reg (再打一拍)
                                              │
                              change_detected = noisy_reg ^ noisy_synced  (变化沿，1 拍脉冲)
                                              │
                                              ▼
                         oh_counter.load = change_detected | ~nreset_synced
                         （输入一变 / 复位一有效 → 计数器清零重启）
                                              │
                         计数到满量程 → wraparound (稳定窗口已到)
                                              │
                                              ▼
                         clean_reg <= noisy_reg   （采样稳定值）
                                              │
                                              ▼
                                          clean_out
```

去抖窗口的长度由两个参数决定：`BOUNCE`（抖动时间）与 `FREQUENCY`（时钟频率）。计数器位宽为

\[ \text{COUNTER\_WIDTH} = \lceil \log_2(\text{BOUNCE}\times\text{FREQUENCY}) \rceil \]

实际溢出周期是 \(2^{\text{COUNTER\_WIDTH}}\) 个时钟周期（向上取整到 2 的幂，略大于 `BOUNCE×FREQUENCY`）。默认参数 `BOUNCE=100, FREQUENCY=1000000` 只是占位示例（算出的窗口极大），实际使用要按真实按键抖动时间（通常 5~20 ms）与真实时钟频率填入。

#### 4.3.3 源码精读

参数与端口在 [oh_debouncer.v:L8-L19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L8-L19)。计数器位宽由 `$clog2` 派生，见 [oh_debouncer.v:L21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L21)。

**第一级：同步。** 用 `oh_dsync` 同步输入、`oh_rsync` 同步复位，见 [oh_debouncer.v:L28-L40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L28-L40)。这正是本讲前置知识里 [u2-l4](u2-l4-cdc-synchronizers.md) 的直接复用。

**第二级：变化检测。** 把同步后的信号再打一拍存入 `noisy_reg`，再与同步值异或，得到 1 拍的变化脉冲，见 [oh_debouncer.v:L43-L49](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L43-L49)：

```verilog
noisy_reg <= noisy_synced;                    // 打一拍
assign change_detected = noisy_reg ^ noisy_synced;  // 异或 = 任意变化沿
```

这与 `oh_edge2pulse` 的第一级完全同构（[u2-l4](u2-l4-cdc-synchronizers.md)）。

**第三级：定时滤波。** 用 `oh_counter` 当计时器，见 [oh_debouncer.v:L52-L66](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L52-L66)：

```verilog
oh_counter #(.N(COUNTER_WIDTH), ...) oh_counter (
   .en(1'b1), .dec(1'b0), .autowrap(1'b0),
   .load(change_detected | ~nreset_synced),   // 一有变化或复位就重载
   .load_data({(COUNTER_WIDTH){1'b0}}),        // 重载为 0
   .wraparound(wraparound)                     // 计满溢出 = 稳定窗口到
);
```

要点（结合 [oh_counter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_counter.v)，[u3-l3](u3-l3-arithmetic-datapath.md) 已讲）：`autowrap=0` 时，计数器计到全 1 会**停住并持续给出 `wraparound=1`**，直到下一次 `load` 把它清零。所以「输入一抖 → 清零；安静到计满 → wraparound 持续有效」。

**第四级：采样。** 只有 `wraparound` 有效时才更新输出，见 [oh_debouncer.v:L69-L75](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L69-L75)：

```verilog
else if(wraparound) clean_reg <= noisy_reg;   // 稳定窗口到，才采信当前电平
assign clean_out = clean_reg;
```

> ⚠️ **注释/参数不匹配（重要）**，两处历史遗留：
>
> 1. [oh_debouncer.v:L16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L16) 注释写 `// syncronous active high reset`，但代码用 `negedge nreset`（[oh_debouncer.v:L43](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_debouncer.v#L43)），实为**异步低有效复位**——与 OH! 全库约定一致，**注释写错了**，以代码为准。
> 2. 配套测试包装 [stdlib/testbench/dut_debouncer.v:L41-L46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_debouncer.v#L41-L46) 用 `oh_debouncer #(.CLKPERIOD(10))` 实例化，但模块**根本没有 `CLKPERIOD` 这个参数**（真实参数是 `BOUNCE/FREQUENCY/SYN/TYPE`）。这个 dut 包装是 stale 的，按现状会参数不匹配。学习时请直接读 `oh_debouncer.v` 本体。

#### 4.3.4 代码实践

**实践目标**：按真实按键算出去抖参数，并跟踪一遍「按下抖动 → 输出延迟更新」的过程。

**操作步骤（参数计算）**：

1. 假设时钟 `FREQUENCY = 50_000_000`（50 MHz），按键抖动时间 `BOUNCE = 10`（单位与频率配合：`BOUNCE×FREQUENCY` 应等于抖动期内的时钟周期数；这里把 `BOUNCE` 理解为「以秒为单位的抖动时间乘以频率」的乘数，即想得到 10 ms 窗口就令 `BOUNCE×FREQUENCY = 500_000`）。
2. 手算 `COUNTER_WIDTH = $clog2(500_000)`。  
   \(2^{18}=262144,\ 2^{19}=524288\)，所以 `COUNTER_WIDTH = 19`。
3. 真实溢出窗口 = \(2^{19}=524288\) 个周期 ≈ 10.49 ms @50 MHz，略大于 10 ms，符合「向上取整」的预期。

**需要观察的现象（源码阅读型）**：跟踪一次「输入从 0 跳到 1 并带抖动」的过程——
- `noisy_in` 抖动 → `change_detected` 反复出脉冲 → `oh_counter.load` 反复有效 → 计数器总被清零，`wraparound` 拿不到；
- 抖动结束后 `change_detected` 停止 → 计数器一路累加到全 1 → `wraparound=1` → `clean_reg` 才把 1 采进输出。

**预期结果**：输出 `clean_out` 相对输入最后一次跳变，延迟约一个去抖窗口才翻转；抖动期间输出纹丝不动。

**待本地验证**：因 `dut_debouncer.v` 的参数名不匹配，直接用仓库脚本跑不通；建议手写一个正确传参（`.BOUNCE()/.FREQUENCY()`）的简易 tb 来仿真。

#### 4.3.5 小练习与答案

**练习 1**：为什么去抖电路要先过 `oh_dsync` 再做过沿检测，而不是直接对 `noisy_in` 做异或？  
**答**：`noisy_in` 是异步信号，直接进边沿检测会引入亚稳态（[u2-l4](u2-l4-cdc-synchronizers.md)）。先用 `oh_dsync` 同步到本时钟域，再在同步后的稳定信号上做异或，才是安全的「先同步、再取沿」范式。

**练习 2**：`oh_counter` 的 `autowrap=0` 在这里起什么作用？如果改成 `autowrap=1` 会怎样？  
**答**：`autowrap=0` 使计数器计满后**停在最大值并持续给出 `wraparound`**，从而让 `clean_reg` 能在稳定期持续采信输入、且任何新抖动都会立刻 `load` 清零重启。若改 `autowrap=1`，计数器计满后会回绕到 0 继续计，`wraparound` 只闪一拍，`clean_reg` 只在那一拍采样，可能漏掉稳定期内的更新——不适合去抖语义。

**练习 3**：默认 `BOUNCE=100, FREQUENCY=1000000` 算出的去抖窗口有多大？为什么说它是占位值？  
**答**：`COUNTER_WIDTH=$clog2(1e8)=27`，溢出周期 \(2^{27}\approx 1.34\times10^8\) 个周期；@1 MHz 约 134 秒——对按键去抖来说长得离谱，显然不是真实设计值，只是演示参数语义的占位值，实际必须按真实抖动时间与时钟频率重填。

---

## 5. 综合实践

把本讲两块内容串成一个完整任务：**「4 路请求仲裁 + grant 展宽成稳定使能」**，模拟 elink 发送端「判决出一个胜者后，把它的授权维持若干拍」的场景。

任务要求：

1. 用 `oh_arbiter #(.N(4))` 对 4 路请求做固定优先级判决，得到 one-hot 的 `grants[3:0]`。
2. 把 **bit0 的 grant** 接到 `oh_stretcher`（取 `CYCLES=4`），把那一拍的授权展宽成 4 拍的 `grant0_stretched`，当作下游的稳定使能。
3. 仿照 [etx_arbiter.v:L98-L111](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v#L98-L111) 的写法，为 4 路请求各产出一个 `wait`（反压）信号：低优先级路在「下游停顿」或「有更高优先级路在请求」时拉高 wait。

参考思路（**示例代码**，仅示意连接关系，省略了声明与复位细节）：

```verilog
oh_arbiter #(.N(4), .TYPE("FIXED")) u_arb (.requests(req), .grants(grant));
oh_stretcher #(.CYCLES(4)) u_str (.clk(clk), .nreset(nreset), .in(grant[0]), .out(grant0_stretched));

// 反压：逐级累积 OR，与 etx_arbiter 同构
assign wait3 = stall | req[0] | req[1] | req[2]; // 最低优先级，最易被挂起
assign wait2 = stall | req[0] | req[1];
assign wait1 = stall | req[0];
assign wait0 = stall;                             // 最高优先级，只看下游
```

完成后请回答：当 `req=4'b0011`、`stall=0` 时，谁拿到 grant？`grant0_stretched` 会高几拍？当 `stall=1` 时，四路 `wait` 各是多少？（答案：bit0 拿到 grant，`grant0_stretched` 高 4 拍；`stall=1` 时四路 wait 全为 1。）

**待本地验证**：完整跑通需要先解决 [u1-l3](u1-l3-simulation-setup.md) 提到的脚本路径问题；若环境暂不可用，至少把框图与信号连接画出来、手工推演一遍时序。

## 6. 本讲小结

- **`oh_arbiter`** 是纯组合的固定优先级仲裁器，核心是 `waitmask[j]=|requests[j-1:0]` 与 `grants=requests & ~waitmask`；**bit0 优先级最高**，优先级由实例化时 `{}` 拼接的位序决定。
- 源码注释里的 `ROUNDROBIN/FAIR` **并未实现**，elink 的 `etx_arbiter` 也留有「TODO: 改成 round robin」的注释——固定优先级有**饥饿**风险，轮询公平但更复杂。
- 仲裁器只给 **grant**，**ready/反压** 需要调用方据下游 wait 自行构造，`etx_arbiter` 的逐级累积 OR 是标准范式。
- **`oh_stretcher`** 用「全置 1 再左移 0」的移位寄存器，把单周期脉冲展宽成约 `CYCLES` 拍，电路极简。
- **`oh_pulse`** 是随机周期脉冲源，但依赖的 **`oh_random` 全仓库未定义**，无法直接编译，是 stdlib 历史「半成品」的一例。
- **`oh_debouncer`** 按「`oh_dsync` 同步 → 异或取沿 → `oh_counter` 重载式计时 → 溢出采样」四步去抖；其注释「同步高有效复位」与 dut 包装的 `.CLKPERIOD` 参数都是**与代码不符的历史遗留**，以源码为准。

## 7. 下一步学习建议

本讲的控制原语是后续「协议层」的积木。建议接着：

1. **进入第 5 单元 emesh 片上网络**：emesh 的 ready 反压、多路 mux/arbiter 正是本讲仲裁器的放大版，读 [emesh/hdl/emesh_mux.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v) 时回想 `oh_arbiter` 的 waitmask 思路。
2. **第 7 单元 elink 高速链路**：本讲引用的 [etx_arbiter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_arbiter.v) 与 `erx_arbiter.v` 是 elink TX/RX 的入口，到时候会完整拆解它们如何把仲裁结果接入 FIFO 与 IO。
3. 想练手的话，给本讲的 `oh_arbiter` **补一个真正的轮询实现**（用一个寄存器记住「上一轮的胜者」，下一轮让它优先级最低），对比固定优先级在公平性上的差异——这正好对应 elink 里那句未完成的 TODO。
