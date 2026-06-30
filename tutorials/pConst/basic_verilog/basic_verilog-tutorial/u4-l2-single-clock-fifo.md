# 单时钟 FIFO：fifo_single_clock_ram

## 1. 本讲目标

FIFO（First In First Out，先进先出队列）是数字设计里最常用的缓冲结构之一：生产者把数据依次「塞进去」，消费者按相同的顺序「取出来」，中间用一块存储体吸收两者速率的不匹配。

本讲以仓库里的 [fifo_single_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv) 为核心，讲清一个**单时钟域、基于双口 RAM 的标准 FIFO** 是怎么搭起来的。读完本讲你应当能：

- 说清「环形指针 + `inc_ptr` 回绕」是如何把一块线性 RAM 当成首尾相连的环来用的；
- 说清为什么用一个多一位的计数器 `cnt` 就能同时、无歧义地判断「满」与「空」；
- 说清当读、写请求**同一拍同时到达**，且 FIFO 正好「满」或「空」时，模块如何仲裁（只读 / 只写 / 既读又写）；
- 说清 `fail` 信号与 `w_req_f`/`r_req_f` 是如何分别向上层报告和向底层 RAM 屏蔽「溢出（overflow）/下溢（underflow）」的；
- 自己写一个 testbench，验证写满、溢出、读空、下扰全过程的波形与标志位时序。

本讲覆盖四个最小模块：**环形指针**、**满空判断**、**同时读写仲裁**、**overflow/underflow 保护**。

## 2. 前置知识

在进入本讲前，你需要具备以下认知（前序讲义已建立）：

- **模块的四段式结构与参数化端口**（u1-l2）：能看懂 `#(parameter ...)` 端口表、`always_ff`/`always_comb` 的分工，以及 `<=`（非阻塞）与 `=`（阻塞）的区别。
- **RAM/ROM 模板：单口与真双口**（u4-l1）：本讲的 FIFO **不自己写存储体**，而是直接例化 [true_dual_port_write_first_2_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_dual_port_write_first_2_clock_ram.sv)。你需要记得：块 RAM 的读是**同步**的（读地址打入后，数据下一拍才出现在 `doutb`）；同址又读又写时，write-first 语义会输出新写入的值。
- **位宽计算 `clogb2` 与 `$clog2`**（u2-l4）：本讲的计数器位宽 `DEPTH_W = clogb2(DEPTH)+1`，你需要记得 `clogb2(n)` 等价于 `$clog2(n+1)`。

几个本讲会用到的通俗概念：

- **FIFO 的「头」与「尾」**：写指针 `w_ptr` 指向下一个该写入的位置（尾），读指针 `r_ptr` 指向下一个该读出的位置（头）。数据总是从尾进、从头出，因而天然先进先出。
- **环形（circular）**：把 RAM 的 `0..DEPTH-1` 号单元想象成排成一圈，地址数到 `DEPTH-1` 后下一次回到 `0`，于是有限的存储空间可以被无限次复用。
- **满与空**：当 FIFO 里一个元素都没有时叫「空」，不能再读；当元素塞满 `DEPTH` 个时叫「满」，不能再写。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [fifo_single_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv) | 本讲主角。单时钟 FIFO 的控制器：维护读写指针与 `cnt` 计数、产生满空标志、仲裁同时读写、保护溢出/下扰，并把数据收发委托给一块双口 RAM。 |
| [true_dual_port_write_first_2_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_dual_port_write_first_2_clock_ram.sv) | 被 FIFO 例化的存储体。A 口专管写、B 口专管读，两端口共享同一块 `data_mem`。本讲把它当作「黑盒存储」。 |
| [fifo_single_clock_ram_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv) | 作者写的 testbench，用随机数 + 扫描方向驱动 DUT，并与 Altera `scfifo` 做对照。本讲的实践任务会参考它的时钟/复位产生方式，但写一个更小、更聚焦的版本。 |
| [clogb2.svh](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh) | 被前两个文件 `` `include `` 的位宽函数，决定 `cnt` 与指针的宽度。 |

## 4. 核心概念与源码讲解

先对模块的端口有一个整体印象。[fifo_single_clock_ram.sv:53-87](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L53-L87) 是参数与端口声明：`DEPTH`（容量，必须为 2 的幂）、`DATA_W`（数据位宽）、写侧 `w_req`/`w_data`、读侧 `r_req`/`r_data`、以及辅助输出 `cnt`/`empty`/`full`/`fail`。

> ⚠️ 一处值得注意的细节：端口表里声明了参数 `FWFT_MODE`（[第 55 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L55)），但 INFO 注释（[第 17 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L17)）和模块主体都表明**本模块只实现了 normal（标准）模式，没有实现 FWFT（first-word-fall-through）模式**，该参数目前在主体中并未被使用。FWFT 进阶变体留到 u4-l3 讲。

### 4.1 环形指针

#### 4.1.1 概念说明

FIFO 的存储体是一块**线性编址**的 RAM（单元编号 `0` 到 `DEPTH-1`），但逻辑上我们希望它是一个**首尾相连的环**：写到最后一个单元后，下一个写位置回到 0；读也一样。实现这个「环」靠的就是两个不断自增、到顶回绕的指针：

- **写指针 `w_ptr`**：指向下一个要写入的单元；
- **读指针 `r_ptr`**：指向下一个要读出的单元。

只要两者都按同一方向、同一回绕规则前进，数据就会沿环依次流动，先进来的先被读走。

#### 4.1.2 核心流程

指针的递增遵循一个统一的回绕规则，记为 `inc_ptr`：

\[
\text{inc\_ptr}(p) = \begin{cases} 0 & \text{若 } p = \text{DEPTH}-1 \\ p+1 & \text{否则} \end{cases}
\]

等价于 \( p \mapsto (p+1) \bmod \text{DEPTH} \)。每完成一次有效写，`w_ptr ← inc_ptr(w_ptr)`；每完成一次有效读，`r_ptr ← inc_ptr(r_ptr)`。「到顶回零」就是环形的全部秘密。

> 为什么要求 `DEPTH` 必须是 2 的幂（见 [INFO 第 57 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L57)）？本模块用的是显式比较 `ptr == DEPTH-1` 再清零的 `inc_ptr`，**并非**靠截断高位来回绕，所以严格说 `inc_ptr` 本身对任意正整数 `DEPTH` 都成立。但双口 RAM 的地址位宽、以及后续很多进阶 FIFO（用指针高位判断满空的那一类）都依赖 2 的幂，作者统一约定 `DEPTH` 为 2 的幂以保持整库一致、避免边界陷阱。

#### 4.1.3 源码精读

指针本身只是两个普通寄存器，复位时清零（[fifo_single_clock_ram.sv:90-92](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L90-L92)）：

```systemverilog
// read and write pointers
logic [DEPTH_W-1:0] w_ptr = INIT_CNT[DEPTH_W-1:0];
logic [DEPTH_W-1:0] r_ptr = '0;
```

回绕逻辑封装成一个函数 `inc_ptr`，到顶回零（[fifo_single_clock_ram.sv:173-181](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L173-L181)）：

```systemverilog
function [DEPTH_W-1:0] inc_ptr (input [DEPTH_W-1:0] ptr);
  if( ptr[DEPTH_W-1:0] == DEPTH-1 ) begin
    inc_ptr[DEPTH_W-1:0] = '0;          // 到顶，回零
  end else begin
    inc_ptr[DEPTH_W-1:0] = ptr[DEPTH_W-1:0] + 1'b1;
  end
endfunction
```

写指针 `w_ptr` 接到 RAM 的 A 口地址 `addra`，读指针 `r_ptr` 接到 B 口地址 `addrb`（[fifo_single_clock_ram.sv:102-121](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L102-L121)），于是「环」的当前位置就直接对应到 RAM 中的物理单元。

#### 4.1.4 代码实践

**实践目标**：直观看到指针回绕。

**操作步骤**：在 [fifo_single_clock_ram_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv) 的 DUT 例化里临时把 `cnt1` 端口换成对内部指针的观察（或直接在波形里展开 `FF1.w_ptr` / `FF1.r_ptr`），用 `DEPTH=8` 跑随机激励一段时间。

**需要观察的现象**：`w_ptr` 与 `r_ptr` 都在 `0..7` 之间递增，数到 `7` 之后下一拍变回 `0`，再从 `0` 继续；两个指针各自独立回绕、互不同步。

**预期结果**：两个指针呈现「锯齿状」周期波形，最大值不超过 `7`，绝不会出现 `8`。（具体波形待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：若把 `inc_ptr` 里的 `DEPTH-1` 误写成 `DEPTH`，会发生什么？
**答案**：指针会数到 `DEPTH`（例如 8）才回零，而 RAM 只有 `0..DEPTH-1` 号单元，于是会去访问一个越界/不存在的地址，破坏环形语义。这正是回绕点必须卡在 `DEPTH-1` 的原因。

**练习 2**：为什么读、写指针可以共用同一个 `inc_ptr` 函数？
**答案**：因为两者沿同一个环、按同一方向、同一回绕规则前进，只是「谁来递增」由当前的读/写事件决定。回绕规则本身与方向无关。

### 4.2 满空判断

#### 4.2.1 概念说明

知道了指针怎么走，下一个问题是：**什么时候该停？** 写到不能再写叫「满」，读到不能再读叫「空」。判断满空有两种经典流派：

1. **比较指针流派**：让指针比地址多 1 位，用两个指针「地址位相等但最高位不同」判满、「完全相等」判空。省一个计数器，但满空逻辑稍绕。
2. **计数器流派**：额外维护一个元素计数 `cnt`，直接 `cnt==0` 为空、`cnt==DEPTH` 为满。逻辑直观，代价是多一组寄存器。

本模块采用的是**计数器流派**——简单、可读、跨厂家一致，这正契合 INFO 所说的「cross-vendor and sim/synth compatibility」（[第 12 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L12)）。

#### 4.2.2 核心流程

`cnt` 是当前 FIFO 里元素的个数，取值范围是闭区间 \([0,\,\text{DEPTH}]\)，共 \(\text{DEPTH}+1\) 种状态。满空判断就是两个整数比较：

\[
\text{empty} \equiv (\text{cnt} = 0),\qquad \text{full} \equiv (\text{cnt} = \text{DEPTH})
\]

关键在于位宽：要能装下「满」这个值，`cnt` 的位数必须能表示 `DEPTH` 本身。表示 \(\text{DEPTH}+1\) 个状态（\(0\) 到 \(\text{DEPTH}\)）至少需要 \(\lceil\log_2(\text{DEPTH}+1)\rceil\) 位。作者用 `DEPTH_W = clogb2(DEPTH)+1`，比下限略宽（对 `DEPTH=8` 得 5 位），是带余量的写法，参见 `clogb2(8)=4`（[clogb2.svh:35](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L35)）。多出来的位就是用来让 `cnt` 取到 `DEPTH`（满）而不溢出的——这就是「多一位编码满/空」在本模块里的体现。

满空标志的组合输出与 `cnt` 的增减配合：写一令 `cnt+1`、读一令 `cnt-1`、同时读写则 `cnt` 不变（见 4.3）。

#### 4.2.3 源码精读

`cnt` 的位宽在参数表里就带上了那个 `+1`（[fifo_single_clock_ram.sv:57-59](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L57-L59)）：

```systemverilog
DEPTH = 8,                 // max elements count == DEPTH, DEPTH MUST be power of 2
DEPTH_W = clogb2(DEPTH)+1, // elements counter width, extra bit to store
                           // "fifo full" state, see cnt[] variable comments
```

满空与失败标志都是**纯组合**输出，直接由 `cnt` 译码（[fifo_single_clock_ram.sv:165-171](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L165-L171)）：

```systemverilog
always_comb begin
  empty = ( cnt[DEPTH_W-1:0] == '0 );     // 一个元素都没有
  full  = ( cnt[DEPTH_W-1:0] == DEPTH );  // 塞满 DEPTH 个

  fail  = ( empty && r_req ) ||           // 读空 FIFO（下溢）
          ( full  && w_req );             // 写满 FIFO（溢出）
end
```

注意 `cnt` 本身在 `always_ff` 里更新（下一节），所以 `empty`/`full` 反映的是**当前已寄存**的元素数；一次让 `cnt` 到达 `DEPTH` 的写入发生后，`full` 会在**下一拍**（`cnt` 变成 `DEPTH` 的那一拍）才升起。`fail` 会在「非法请求」当拍组合地给出，留给上层处理（4.4 详述）。

#### 4.2.4 代码实践

**实践目标**：在波形里数清「写几个会满、读几个会空」。

**操作步骤**：保持 `DEPTH=8`，用本讲末尾「综合实践」给出的最小 testbench（或随机 tb），在波形里同时显示 `cnt`、`empty`、`full`。

**需要观察的现象**：从复位态 `cnt=0, empty=1, full=0` 开始；每写入一个 `cnt` 加 1，`empty` 在 `cnt` 离开 0 的下一拍落下；写到第 8 个使 `cnt=8` 时 `full` 升起；反向读出时 `cnt` 递减，`full` 先落、读空时 `empty` 再起。

**预期结果**：`cnt` 始终在 `0..8` 之间，`empty` 与 `full` 永远不会同时为 1。（逐拍数值待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cnt` 必须能表示 `DEPTH`，而不仅是 `DEPTH-1`？
**答案**：因为「满」的定义是「装了 DEPTH 个元素」，`cnt` 必须能取到 `DEPTH` 这个值。若位数只够表示到 `DEPTH-1`，就无法把「满」与「装有 DEPTH-1 个」区分开。

**练习 2**：如果把满空判断从「计数器流派」换成「比较指针流派」，最大的取舍是什么？
**答案**：比较指针流派省掉了 `cnt` 寄存器（面积更省），但满空逻辑要比较两个多一位的指针、判断「地址相等且最高位相反」，可读性差、更容易写错。本模块选计数器流派，是用一点寄存器开销换取直观与跨工具一致性。

### 4.3 同时读写仲裁

#### 4.3.1 概念说明

FIFO 有两个独立的端口：一个写、一个读。它们完全可能在**同一个时钟沿**同时来请求（`w_req=1` 且 `r_req=1`）。大多数时候这没问题——既写又读，元素总数 `cnt` 不变。但当 FIFO 正好处在「空」或「满」的边界时，就有冲突：

- **空时同时读写**：里面一个元素都没有，怎么可能「同时读」？只能放弃读、只执行写。
- **满时同时读写**：已经塞满，怎么可能「同时写」？只能放弃写、只执行读。

INFO 把这套规则写得很明确（[fifo_single_clock_ram.sv:19-22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L19-L22)）：从满 FIFO 同时读写只做读、从空 FIFO 同时读写只做写，并提醒使用者「Always honor empty and full flags!」。

#### 4.3.2 核心流程

模块用一个 `unique case ({w_req, r_req})` 把四种请求组合展开，并在 `2'b11`（同时读写）这一支里再用 `empty`/`full` 做二次仲裁。完整规则表如下：

| `w_req` | `r_req` | 条件 | `w_ptr` | `r_ptr` | `cnt` | 含义 |
|:---:|:---:|:---|:---:|:---:|:---:|:---|
| 0 | 0 | — | 不变 | 不变 | 不变 | 空闲 |
| 0 | 1 | `~empty` | 不变 | +1 | −1 | 只读 |
| 1 | 0 | `~full` | +1 | 不变 | +1 | 只写 |
| 1 | 1 | `empty` | +1 | 不变 | +1 | 同时请求但空 → **只写** |
| 1 | 1 | `full` | 不变 | +1 | −1 | 同时请求但满 → **只读** |
| 1 | 1 | 其他 | +1 | +1 | 不变 | 既读又写，总数不变 |

最右边三行是同一支 `2'b11` 内的三种分支，正是本节的仲裁核心。

> 一个精妙的细节：`cnt` 在「既读又写」时**故意不更新**（源码里那行 `//cnt` 被注释掉了，[第 158 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L158)）。因为一个进、一个出，元素总数本来就不变，省掉这次加减法也让 `cnt` 的翻转活动更少、利于低功耗与时序。

#### 4.3.3 源码精读

整段指针/计数维护逻辑在一个 `always_ff` 里（[fifo_single_clock_ram.sv:124-163](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L124-L163)），关键是同时读写那一支：

```systemverilog
2'b11: begin  // simultaneously reading and writing
  if( empty ) begin                 // 空：只写
    w_ptr[DEPTH_W-1:0] <= inc_ptr(w_ptr[DEPTH_W-1:0]);
    cnt[DEPTH_W-1:0]   <= cnt[DEPTH_W-1:0] + 1'b1;
  end else if( full ) begin         // 满：只读
    r_ptr[DEPTH_W-1:0] <= inc_ptr(r_ptr[DEPTH_W-1:0]);
    cnt[DEPTH_W-1:0]   <= cnt[DEPTH_W-1:0] - 1'b1;
  end else begin                    // 正常：既读又写，cnt 不变
    w_ptr[DEPTH_W-1:0] <= inc_ptr(w_ptr[DEPTH_W-1:0]);
    r_ptr[DEPTH_W-1:0] <= inc_ptr(r_ptr[DEPTH_W-1:0]);
    //cnt[DEPTH_W-1:0] <=  // data counter does not change here
  end
end
```

注意这里的 `case` 用的是**原始** `{w_req, r_req}`，而每一支内部再用 `~empty`/`~full` 做二次判断。这与下一节 RAM 侧用 `w_req_f`/`r_req_f` 屏蔽是**两套对齐的保护**——指针/计数的更新规则，与 RAM 实际是否使能，最终由同一组 `empty`/`full` 驱动，保持完全一致（详见 4.4.3）。

#### 4.3.4 代码实践

**实践目标**：构造「同时读写 + 边界」场景，验证仲裁规则。

**操作步骤**：写一个最小 testbench，先写满 `DEPTH=8`，然后在**同一拍**同时拉高 `w_req` 和 `r_req` 并维持若干拍；再读空后同样维持同时读写若干拍。用 `$display` 打印每拍的 `cnt`/`empty`/`full`。

**需要观察的现象**：满状态下同时读写，`cnt` 应每拍 **−1**（只读，写被拒）；空状态下同时读写，`cnt` 应每拍 **+1**（只写，读被拒）；非边界同时读写，`cnt` 保持不变。

**预期结果**：`cnt` 的变化方向严格符合上表最后一列。（具体逐拍数值待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么「空时同时读写」选择只写、而不是直接报错？
**答案**：写是把新元素存入，逻辑上自洽（FIFO 从无到有，变 1 个）；而读要取出一个不存在的元素，无意义。模块选择「尽量完成能完成的那一侧」，再用 `fail` 提示被拒绝的一侧，比直接卡死更健壮。

**练习 2**：`unique case` 里的 `unique` 关键字在这里起什么作用？
**答案**：`unique` 要求所有分支互斥且至少命中一条，综合/仿真器会检查「同一时刻只命中一个分支」。这里四条分支 `{2'b00,2'b01,2'b10,2'b11}` 天然互斥，加 `unique` 可在仿真期尽早发现「意外落入多支或无支」的编码错误。

### 4.4 overflow/underflow 保护

#### 4.4.1 概念说明

理想的 FIFO 使用者总会先看 `empty`/`full` 再决定读写，但现实里总会出现「忘了看」的情况：对满 FIFO 继续写叫**溢出（overflow）**，对空 FIFO 继续读叫**下溢（underflow）**。一个健壮的 FIFO 必须**即使被错误使用也不损坏内部数据**，并尽可能**告诉上层发生了错误**。

本模块用了两层防护：

1. **向底层 RAM 屏蔽非法请求**：用 `w_req_f = w_req && ~full`、`r_req_f = r_req && ~empty` 把非法请求过滤掉再送到 RAM 的使能端口，保证 RAM 永远不会被错误地写穿或空读。
2. **向上层报告非法请求**：用组合信号 `fail` 在当拍指出「发生了 overflow 或 underflow」，供上层做统计或告警。

#### 4.4.2 核心流程

两条「过滤后」的请求是纯组合：

\[
\text{w\_req\_f} = \text{w\_req} \wedge \neg\text{full},\qquad \text{r\_req\_f} = \text{r\_req} \wedge \neg\text{empty}
\]

只有 `w_req_f` 才会真正驱动 RAM 的 A 口写使能 `ena`，只有 `r_req_f` 才会驱动 B 口读使能 `enb`。失败报告则是：

\[
\text{fail} = (\text{empty} \wedge \text{r\_req}) \vee (\text{full} \wedge \text{w\_req})
\]

即「空却要读」或「满却要写」。`fail` 与过滤使能是同一组条件的两种用法：一个用来「阻止」，一个用来「上报」。

> 注意读时序：本模块只实现 normal 模式，读数据 `r_data` 来自双口 RAM 的 `doutb`，而块 RAM 的读是**同步**的——`r_req_f` 当拍把地址打入，数据**下一拍**才出现在 `r_data` 上（见 [true_dual_port_write_first_2_clock_ram.sv:105-118](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_dual_port_write_first_2_clock_ram.sv#L105-L118)）。因此读出比请求晚一拍，这是标准 FIFO 与 FWFT FIFO 的关键区别（FWFT 见 u4-l3）。

#### 4.4.3 源码精读

过滤使能的定义（[fifo_single_clock_ram.sv:94-99](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L94-L99)）：

```systemverilog
// filtered requests
logic w_req_f;
assign w_req_f = w_req && ~full;     // 满则不写

logic r_req_f;
assign r_req_f = r_req && ~empty;    // 空则不读
```

它们被接到 RAM 的使能端，`wea=1'b1`（A 口恒为写）、`web=1'b0`（B 口恒为读）（[fifo_single_clock_ram.sv:102-121](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L102-L121)）：

```systemverilog
true_dual_port_write_first_2_clock_ram #(
  .RAM_WIDTH( DATA_W ), .RAM_DEPTH( DEPTH ), ...
) data_ram (
  .clka( clk ), .addra( w_ptr[DEPTH_W-1:0] ),
  .ena( w_req_f ), .wea( 1'b1 ), .dina( w_data[DATA_W-1:0] ),   // A 口 = 写端口
  ...
  .clkb( clk ), .addrb( r_ptr[DEPTH_W-1:0] ),
  .enb( r_req_f ), .web( 1'b0 ), .doutb( r_data[DATA_W-1:0] )   // B 口 = 读端口
);
```

而 `fail` 的组合定义已在 4.2.3 引用（[第 169-170 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L169-L170)）。现在可以验证「两层防护一致性」：以「同时读写且满」为例，`case` 的 `2'b11/full` 分支只更新 `r_ptr`、`cnt−1`（4.3.3）；与此同时 `w_req_f = w_req && ~full = 0`，A 口写使能被关掉，RAM 只读不写。**指针逻辑与 RAM 行为完全对齐**，数据不会被错误地写穿。其余几种边界同理可一一验证。

#### 4.4.4 代码实践

**实践目标**：触发 overflow 与 underflow，观察 `fail` 与数据保护。

**操作步骤**：在 `DEPTH=8` 的 FIFO 上，复位后**连续写 12 个**（明显超过容量），再**连续读 12 个**（明显超过存量），用 `$display` 监测 `full`/`fail`/`cnt`/`r_data`。

**需要观察的现象**：前 8 次写正常，`cnt` 到 8 后 `full=1`；第 9~12 次写时 `fail=1`、`cnt` 不再增长（写被屏蔽，数据未损坏）。读出阶段前 8 次按先进先出顺序拿到当初写入的值，第 9~12 次读时 `fail=1`（空读被屏蔽）。

**预期结果**：`fail` 恰好在「满写」「空读」的那些拍为 1；最终 FIFO 里既没多存也没丢数据。（逐拍精确数值待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：如果去掉 `w_req_f`/`r_req_f`，把 `w_req`/`r_req` 直接接 RAM 使能，会出什么问题？
**答案**：满 FIFO 上继续写会覆盖「还没被读走的」有效数据（写穿）；空 FIFO 上继续读会读出过期/无效数据并可能让 `r_ptr` 越过 `w_ptr` 破坏环形结构。过滤使能是保护存储体不被错误使用的关键一环。

**练习 2**：`fail` 为什么设计成组合（当拍有效）而不是寄存输出？
**答案**：`fail` 是 `empty`/`full`（由 `cnt` 译码）与当拍 `w_req`/`r_req` 的组合函数，当拍就能确定「这一拍是否发生了非法请求」，组合输出延迟最低、最及时。若寄存输出，错误信息会晚一拍，且需要额外时钟，不利于上层及时统计。

## 5. 综合实践

把四个最小模块串起来，完成规格里要求的核心任务：**写 testbench，连续向 `DEPTH=8` 的 FIFO 写入 12 个数据，再全部读出，观察 `full`/`fail` 何时置位，以及读出顺序是否先进先出。**

下面是一个**示例代码**（不是仓库原有文件，需要你新建为 `fifo_overflow_tb.sv` 并自行编译）。它的时钟/复位产生风格参考了 [fifo_single_clock_ram_tb.sv:14-52](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L14-L52)，但激励改成确定性的「先写 12、再读 12」，便于和理论值对照：

```systemverilog
`timescale 1ns/1ps
module fifo_overflow_tb;                       // 示例代码

  logic clk = 1'b0;
  always #5 clk = ~clk;                        // 100 MHz

  logic rst = 1'b1;                            // 高有效复位，与 nrst 反相
  initial begin
    #20 rst = 1'b0;                            // 释放复位
  end
  logic nrst; assign nrst = ~rst;

  logic        w_req, r_req;
  logic [15:0] w_data, r_data;
  logic [4:0]  cnt;
  logic        empty, full, fail;

  fifo_single_clock_ram #(
    .FWFT_MODE( "FALSE" ),
    .DEPTH( 8 ),
    .DATA_W( 16 )
  ) dut (
    .clk( clk ), .nrst( nrst ),
    .w_req( w_req ), .w_data( w_data ),
    .r_req( r_req ), .r_data( r_data ),
    .cnt( cnt ), .empty( empty ), .full( full ), .fail( fail )
  );

  integer i;
  initial begin
    w_req = 1'b0; r_req = 1'b0; w_data = 16'd0;
    @(negedge rst);                            // 等复位释放
    @(posedge clk);
    // === 连续写 12 个（超过 DEPTH=8）===
    for (i=0; i<12; i=i+1) begin
      w_data = i[15:0];                        // 写入 0,1,2,...,11
      w_req  = 1'b1;
      @(posedge clk);
    end
    w_req = 1'b0;
    // === 全部读出（读 12 个）===
    for (i=0; i<12; i=i+1) begin
      r_req = 1'b1;
      @(posedge clk);
    end
    r_req = 1'b0;
    #100 $finish;
  end

  // 每拍打印关键信号，便于和理论对照
  always @(posedge clk)
    $display("t=%0t cnt=%0d empty=%b full=%b fail=%b w_req=%b r_req=%b r_data=%0d",
             $time, cnt, empty, full, fail, w_req, r_req, r_data);
endmodule
```

**操作步骤**：

1. 把上面的 testbench 存为 `fifo_overflow_tb.sv`，与 `fifo_single_clock_ram.sv`、`true_dual_port_write_first_2_clock_ram.sv`、`clogb2.svh` 放在同一目录（或设置好 `+incdir+`）。
2. 用 iverilog 编译：`iverilog -g2012 -o sim.vvp fifo_overflow_tb.sv fifo_single_clock_ram.sv true_dual_port_write_first_2_clock_ram.sv`，再 `vvp sim.vvp`；或用 ModelSim `vlog`/`vsim` 跑。
3. 观察打印与波形。

**需要观察的现象与预期结果**（逐拍精确数值待本地验证，但定性结论应如下）：

- **写入阶段**：`cnt` 从 0 递增到 8；第 8 次写入后 `cnt=8`、`full` 升起；第 9~12 次写入时 `full=1`、`fail=1`，`cnt` 不再增长——**只有前 8 个值（0..7）真正被存下**。
- **读出阶段**：因为 normal 模式读数据晚一拍，`r_data` 依次出现 `0,1,2,...,7`（先进先出顺序）；读完 8 个后 `cnt=0`、`empty` 升起；第 9~12 次读时 `empty=1`、`fail=1`，`r_data` 不再有效。
- **结论**：FIFO 严格保序，溢出的 4 次写被 `w_req_f` 屏蔽、下扰的 4 次读被 `r_req_f` 屏蔽，`fail` 精确标记了每一次非法请求，存储体未被损坏。

如果你想让任务更贴近仓库风格，可以打开 [fifo_single_clock_ram_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv) 里被注释掉的 `` `define TEST_SWEEP yes ``（[第 97 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L97)），用作者写好的「写满→反转方向→读空→再反转」扫描逻辑（[第 108-126 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L108-L126)）自动反复压满/抽空 FIFO，长时间观察满空标志与 `fail`。

## 6. 本讲小结

- **环形指针**：`w_ptr`/`r_ptr` 各自按 `inc_ptr`（到 `DEPTH-1` 回零）递增，把线性 RAM 当成首尾相连的环，实现先进先出。
- **满空判断**：用计数器 `cnt`（范围 `0..DEPTH`）直接译码，`empty=(cnt==0)`、`full=(cnt==DEPTH)`；`DEPTH_W = clogb2(DEPTH)+1` 的多一位保证 `cnt` 能取到「满」值。
- **同时读写仲裁**：`unique case ({w_req,r_req})` 处理四种请求；`2'b11` 时若空则只写、若满则只读、否则既读又写且 `cnt` 不变。
- **overflow/underflow 保护**：`w_req_f`/`r_req_f` 过滤后驱动 RAM 使能，阻止写穿/空读；`fail` 组合输出在当拍报告「满写」或「空读」。指针逻辑与 RAM 行为由同一组 `empty`/`full` 驱动，两层防护完全一致。
- **读时序**：normal 模式下 `r_data` 比请求晚一拍（块 RAM 同步读）；FWFT 变体留待 u4-l3。
- **设计取舍**：计数器流派牺牲一点寄存器面积，换来直观、可读、跨厂家一致的满空逻辑，契合本模块「cross-vendor and sim/synth compatibility」的定位。

## 7. 下一步学习建议

本讲解的是**标准（normal）模式**的单时钟 FIFO，读数据要等一拍。下一步建议进入 **u4-l3「FIFO 进阶：FWFT、预读与组合」**，阅读 [fifo_single_clock_reg_v2.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_reg_v2.sv)、[preview_fifo.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv)、[read_ahead_buf.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/read_ahead_buf.sv)，理解 first-word-fall-through 如何让「读请求当拍即出数据」，以及 `fifo_combiner`/`fifo_operator` 如何把多个 FIFO 聚合成一个。届时你会反过来更清楚本讲这套 normal 模式 + `cnt` 计数器的设计在时序上的代价与优势。
