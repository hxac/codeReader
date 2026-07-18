# 原生内存接口与传输状态机

## 1. 本讲目标

本讲打开 PicoRV32 与外部世界打交道的「那扇门」——内存接口。读完本讲，你应当能够：

- 说清 `mem_valid` / `mem_ready` 这对握手信号如何界定一次完整的读或写传输。
- 解释 4 位 `mem_wstrb`（字节写使能）的 8 种合法取值各自对应「不写 / 写字 / 写半字 / 写单字节」中的哪一种。
- 画出内部 `mem_state`（0/1/2/3）四状态机的转移图，并指出每个状态分别驱动读、写、预取中的哪一种事务。
- 理解 Look-Ahead 接口（`mem_la_*`）为何能比普通接口提前一拍给出地址与读写意图，以及这种「提前」带来的性能收益与时序代价。
- 能动手写一段 Verilog，把 `picorv32` 的 `mem_*` 端口接到一个简单 RAM 上，并区分「普通接口（两拍）」与「Look-Ahead 接口（单拍）」两种接法。

本讲承接 [u4-l2 主状态机](u4-l2-main-fsm.md)：主状态机只负责「现在该取指、读数据还是写数据」（用 `mem_do_rinst` / `mem_do_rdata` / `mem_do_wdata` / `mem_do_prefetch` 四个请求标志表达），而真正「把地址打到总线上、等握手、收发数据」的细节，全部交给本讲要讲的内存接口与 `mem_state` 状态机。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是 valid-ready 握手

valid-ready（有效-就绪）是数字电路里最常见的一对握手信号，用来在两个模块之间可靠地传递一次「事务」：

- **valid**（有效）：发起方拉高，表示「我这次给出的地址/数据是有效的，请你处理」。
- **ready**（就绪）：接收方拉高，表示「我这次能接住」。
- **只有当 valid 和 ready 在同一拍同时为高，这一次事务才算完成。**

它最大的好处是天然支持「等待」：如果接收方还没准备好，可以把 ready 压低，发起方就老老实实举着 valid 等着，所有输出信号在等待期间保持稳定。PicoRV32 的原生内存接口就是这种风格——一次只能进行一笔事务（不是流水线化的总线）。

### 2.2 字节写使能（byte write enable, BWE）

很多初学者以为「写内存」只能整字（32 位）一起写。但 RISC-V 里有 `sb`（写一字节）、`sh`（写两字节）、`sw`（写四字节）三种 store 指令。如果每次都「读出整个字、改掉其中的字节、再写回」，既慢又麻烦。字节写使能用 4 根线（对应一个 32 位字的 4 个字节）一次性表达「这次写哪几个字节」：某一位为 1，对应字节就被改写；为 0 就保持原样。PicoRV32 用 `mem_wstrb[3:0]` 这 4 位做这件事。

### 2.3 冯·诺依曼与「提前一拍」

PicoRV32 是冯·诺依曼结构：指令和数据共用同一路内存接口（`mem_instr` 一位用来区分这次是取指还是访存）。CPU 每条指令都要先从内存「取」回来才能执行，所以取指/访存的快慢直接决定 CPI。

设想 RAM 读数据需要一个时钟周期才能给出结果。如果 CPU 在第 N 拍才把地址送到 `mem_addr`，那么数据最早第 N+1 拍才到——这笔事务至少花两拍。但如果 CPU 能在第 N−1 拍就「预告」下一拍的地址，RAM 就可以提前一拍开始读，到第 N 拍 `mem_valid` 拉高时数据已经备好，事务一拍就完成。这就是 Look-Ahead（前瞻）接口的动机。代价是：这些「预告」信号是组合逻辑直接算出来的，处在更长的组合路径上，更难过时序收敛（频率上不去）。

## 3. 本讲源码地图

本讲只涉及两个文件，但要在 `picorv32.v` 里来回看几段：

| 文件 | 作用 |
| --- | --- |
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | 内存接口的端口声明、内部 `mem_state` 状态机、Look-Ahead 组合输出、`mem_wordsize` 字节写使能生成，全部集中在这里。 |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | 用半页篇幅给出了接口的「官方契约」——读/写时序、`mem_wstrb` 的 8 种合法值、Look-Ahead 的含义与注意事项。 |

此外，两个测试台是最好的「使用范例」：

| 文件 | 作用 |
| --- | --- |
| [testbench_ez.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v) | 用**普通接口**（寄存器化的 `mem_ready`）把 CPU 接到内存，是两拍握手的典型写法。 |
| [dhrystone/testbench.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/dhrystone/testbench.v) | 用 **Look-Ahead 接口**（`mem_la_*`）实现单拍内存响应，是追求最高性能时的接法。 |

---

## 4. 核心概念与源码讲解

### 4.1 valid-ready 握手与字节写使能

#### 4.1.1 概念说明

PicoRV32 对外的「原生内存接口」是一组非常精简的信号。可以把 CPU 想象成一个只会做一件事的客人：「我要读某个地址」或「我要往某个地址写某些字节」，而内存（或总线桥）根据 `mem_ready` 告诉它「好了，这次我接住了」。这套接口有如下特点：

1. **一次一笔**：任意时刻最多只有一笔未完成的事务，不是流水线总线。
2. **valid 由 CPU 发、ready 由对端回**：CPU 拉高 `mem_valid` 后会一直保持，地址/数据稳定不变，直到对端回一个 `mem_ready`，这一拍才算成交。
3. **读用 `mem_wstrb=0` 标记，写用 `mem_wstrb≠0` 标记**：`mem_wstrb` 既是「字节写使能」，也兼任「读/写区分位」。
4. **`mem_instr` 区分取指与访存**：同一口总线既要取指令又要读写数据，CPU 用这一位告诉对端「这次是取指」，便于缓存/统计区分。

#### 4.1.2 核心流程

一次**读传输**的时序可以写成：

```
CPU:   把地址送到 mem_addr；mem_wstrb=0；拉高 mem_valid
       (保持 mem_valid 与所有输出稳定)
对端:  读 mem_addr，把结果送到 mem_rdata；拉高一拍 mem_ready
成交:  mem_valid && mem_ready 同拍为高 → 读到的 mem_rdata 被取走，事务结束
```

一次**写传输**的时序：

```
CPU:   把地址送 mem_addr、数据送 mem_wdata、字节掩码送 mem_wstrb(≠0)；拉高 mem_valid
对端:  按 mem_wstrb 把 mem_wdata 的对应字节写进 mem_addr；拉高一拍 mem_ready
成交:  mem_valid && mem_ready 同拍为高 → 写入生效，事务结束
```

`mem_wstrb` 的 4 位 `b3 b2 b1 b0` 分别对应一个 32 位字里的字节 3/2/1/0（字节 0 是最低字节）。README 明确列出它只有 **8 种合法取值**：

| `mem_wstrb` | 含义 | 对应 RISC-V 指令 |
| --- | --- | --- |
| `0000` | 不写（纯读） | 任何 load |
| `1111` | 写整个 32 位字 | `sw` |
| `1100` | 写高 16 位（字节 2、3） | `sh` 且地址最低位为 1 |
| `0011` | 写低 16 位（字节 0、1） | `sh` 且地址最低位为 0 |
| `1000` / `0100` / `0010` / `0001` | 写单字节 3 / 2 / 1 / 0 | `sb`（按地址低 2 位选择） |

注意 `sb`/`sh` 只会产生上面这几种「连续 1」的掩码，不会出现 `0101` 这种值——这是 RISC-V 访存指令的语义决定的。

#### 4.1.3 源码精读

接口信号在模块端口列表里集中声明。原生内存接口只有 7 根：

[picorv32.v:93-100](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L93-L100) 声明了 `mem_valid`/`mem_instr`（CPU 输出）、`mem_ready`（对端输入）、`mem_addr`/`mem_wdata`/`mem_wstrb`（CPU 输出）、`mem_rdata`（对端输入）。紧随其后的就是 Look-Ahead 接口：

[picorv32.v:102-107](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L102-L107) 给出 `mem_la_read`/`mem_la_write`/`mem_la_addr`/`mem_la_wdata`/`mem_la_wstrb` 五根前瞻信号（4.3 节详讲）。

握手的核心定义在一句组合赋值里——「一次传输完成」就叫 `mem_xfer`：

[picorv32.v:373](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L373)

```verilog
assign mem_xfer = (mem_valid && mem_ready) || (mem_la_use_prefetched_high_word && mem_do_rinst);
```

第一项 `(mem_valid && mem_ready)` 就是上面说的「valid 与 ready 同拍为高即成交」；第二项是压缩指令集（C 扩展）里复用已预取高半字时的「免传输」捷径，非压缩配置下恒为 0。整个内存接口的状态机都围绕 `mem_xfer` 这个「成交脉冲」推进。

字节写使能的生成在一段 `case (mem_wordsize)` 的组合逻辑里，`mem_wordsize` 由主状态机按指令类型设为 0（字）/1（半字）/2（字节）：

[picorv32.v:401-428](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L401-L428)（关键三支）：

```verilog
0: begin                              // 字 (sw / lw)
    mem_la_wdata = reg_op2;
    mem_la_wstrb = 4'b1111;
end
1: begin                              // 半字 (sh / lh)
    mem_la_wdata = {2{reg_op2[15:0]}};             // 把半字复制到高低两半
    mem_la_wstrb = reg_op1[1] ? 4'b1100 : 4'b0011; // 按地址 bit1 选高/低半字
end
2: begin                              // 字节 (sb / lb)
    mem_la_wdata = {4{reg_op2[7:0]}};              // 把字节复制 4 份
    mem_la_wstrb = 4'b0001 << reg_op1[1:0];        // 按地址低 2 位选 1 个字节
end
```

这里的两个技巧值得记住：

- **写数据广播**：`{2{...}}` / `{4{...}}` 把要写的半字/字节复制到 32 位的所有对应位置。这样无论 `mem_wstrb` 选中哪几个字节，`mem_wdata` 里那一格的数据都是对的——内存端只需「按掩码写入」即可，不必再做对齐。
- **掩码由地址低位决定**：写哪个字节/半字完全由 `reg_op1`（算好的访存地址）的最低 1~2 位决定，这正是上表中 `mem_wstrb` 取值的来源。

这段组合逻辑算出来的是 Look-Ahead 版本的 `mem_la_wstrb`/`mem_la_wdata`；它们在状态机里（见 4.2.3）被寄存一拍变成对外的 `mem_wstrb`/`mem_wdata`，并在读事务时被屏蔽成 0。

`mem_instr` 的语义在状态机里落实（4.2.3 节会看到 `mem_instr <= mem_do_prefetch || mem_do_rinst`），即「取指或预取指令」时才拉高，纯数据 load/store 时为 0，与 README 的描述一致。README 对外契约的原文见：

[README.md:391-405](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L391-L405)（读传输）与 [README.md:407-420](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L407-L420)（写传输与 `mem_wstrb` 的 8 种合法值）。

#### 4.1.4 代码实践

**目标**：用一个最小测试台验证「读 = `mem_wstrb` 为 0」「写 = `mem_wstrb` 为 8 种合法值之一」。

**步骤**：

1. 阅读 [testbench_ez.v:72-85](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L72-L85)，看清它是如何用普通接口接内存的：每拍先把 `mem_ready` 清 0，看到 `mem_valid && !mem_ready` 就下一拍回 `mem_ready=1` 并按 `mem_wstrb` 逐字节写入。
2. 关注它对 `mem_wstrb` 的处理：

   ```verilog
   if (mem_wstrb[0]) memory[mem_addr >> 2][ 7: 0] <= mem_wdata[ 7: 0];
   if (mem_wstrb[1]) memory[mem_addr >> 2][15: 8] <= mem_wdata[15: 8];
   if (mem_wstrb[2]) memory[mem_addr >> 2][23:16] <= mem_wdata[23:16];
   if (mem_wstrb[3]) memory[mem_addr >> 2][31:24] <= mem_wdata[31:24];
   ```

   这正是「按 4 位掩码分别改写 4 个字节」的最直接实现。
3. 运行 `make test_ez`，观察打印里 `ifetch`（取指，`mem_instr=1`，`wstrb=0000`）、`write`（`wstrb` 非零）、`read`（`wstrb=0000`，`mem_instr=0`）三类事务。

**需要观察的现象**：取指与 `lw` 都打印为「读」类（`wstrb` 为 0），只有 `sw` 打印为「写」并带上非零 `wstrb`；由于示例里是 `sw x_,0(x1)`（字写），`wstrb` 应恒为 `1111`。

**预期结果**：输出形如连续的 `ifetch 0x00000000`、`write 0x000003fc: 0x00000000 (wstrb=1111)`、`read 0x000003fc` 交替。若你看到的 `wstrb` 出现了 8 种合法值以外的值，说明 CPU 或你的接线有错。

> 若本地未装 iverilog，本步骤为「待本地验证」；源码阅读部分（步骤 1-2）不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mem_wstrb` 的取值里没有 `0101` 或 `0110`？
**答案**：因为 RISC-V 的 `sb` 只写一个字节（4 位里只有 1 个 1）、`sh` 写连续两个字节（`0011` 或 `1100`）、`sw` 写满（`1111`）、`load` 不写（`0000`）。没有任何一条指令会产生「隔位」或「非连续」的掩码。

**练习 2**：若对端永远不拉高 `mem_ready`，CPU 会怎样？
**答案**：`mem_xfer` 永远为 0，`mem_state` 卡在 1 或 2，`mem_valid` 一直举着，主状态机拿不到指令/数据，整机停转（但不是 `trap`——`trap` 是 CPU 内部主动进入的死锁，而这是被外部「饿死」）。

---

### 4.2 mem_state 传输状态机

#### 4.2.1 概念说明

`mem_valid`/`mem_ready` 是对外的「契约」，但 CPU 内部还需要一个状态机来管「什么时候拉高 `mem_valid`、什么时候撤掉、什么时候算这笔事务彻底做完」。这就是 2 位的 `mem_state`。它和主状态机 `cpu_state`（[u4-l2](u4-l2-main-fsm.md)）是两个**不同层级**的状态机：

- `cpu_state`：宏观调度，「现在该取指 / 读源寄存器 / 执行 / 访存」，每条指令走一遍。
- `mem_state`：微观总线驱动，「这一拍要不要把 `mem_valid` 举起来、要不要撤掉」，专门伺候内存接口。

两者通过 4 个**请求标志**耦合：主状态机把「我想干什么」写进 `mem_do_prefetch` / `mem_do_rinst` / `mem_do_rdata` / `mem_do_wdata`，`mem_state` 状态机读这些标志来决定动作。

#### 4.2.2 核心流程

`mem_state` 有 4 个状态，职责清晰：

| 状态 | 名称 | 职责 | `mem_valid` |
| --- | --- | --- | --- |
| 0 | 空闲 | 等待请求；把 Look-Ahead 的地址/数据寄存成对外的 `mem_addr`/`mem_wdata`/`mem_wstrb` | 0 |
| 1 | 读在途 | 已为「读」拉高 `mem_valid`，等 `mem_ready` 成交 | 1 |
| 2 | 写在途 | 已为「写」拉高 `mem_valid`，等 `mem_ready` 成交 | 1 |
| 3 | 预取暂存 | 预取的指令已读回但暂不被消费，等真正需要时（`mem_do_rinst`）回空闲 | 0 |

转移规则（非压缩、最简情形）：

```
状态 0：
  若 mem_do_prefetch/rinst/rdata（要读） → 拉高 mem_valid、mem_instr、mem_wstrb=0 → 状态 1
  若 mem_do_wdata（要写）                → 拉高 mem_valid、mem_wstrb≠0            → 状态 2
状态 1（读在途）：
  若 mem_xfer（成交）：
       若是预取且暂不消费（仅 mem_do_prefetch） → 状态 3
       否则（rinst / rdata）                      → 状态 0
状态 2（写在途）：
  若 mem_xfer（成交） → 撤 mem_valid → 状态 0
状态 3（预取暂存）：
  若 mem_do_rinst（终于要用这条指令了） → 状态 0
```

一句话总结：**读走 0→1→（3 或 0），写走 0→2→0**；`mem_xfer` 是所有「成交后离开」转移的发令枪。

#### 4.2.3 源码精读

先看内部寄存器与请求标志的声明：

[picorv32.v:351-358](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L351-L358) 声明了 2 位 `mem_state`、`mem_wordsize`、`mem_rdata_word`/`mem_rdata_q`（读回数据的缓冲与锁存），以及四个请求标志 `mem_do_prefetch`/`mem_do_rinst`/`mem_do_rdata`/`mem_do_wdata`。

`mem_xfer`（成交）与「忙/完成」信号的定义见 [picorv32.v:373-377](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L373-L377)：

```verilog
assign mem_xfer = (mem_valid && mem_ready) || (mem_la_use_prefetched_high_word && mem_do_rinst);
wire mem_busy = |{mem_do_prefetch, mem_do_rinst, mem_do_rdata, mem_do_wdata};
wire mem_done  = resetn && ((mem_xfer && |mem_state && (mem_do_rinst || mem_do_rdata || mem_do_wdata))
                          || (&mem_state && mem_do_rinst)) && (...);
```

`mem_busy` 只要有任何一个请求标志为 1 就为真；`mem_done` 则表示「这一笔请求已经彻底满足，可以清掉请求标志、让主状态机往下走」——它在 `mem_xfer` 发生且 `mem_state≠0` 时（或预取暂存的 `&mem_state` 即 state 3 被消费时）拉高。

主状态机里请求这些动作的地方：取指请求在 `cpu_state_fetch` 里直接置位：

[picorv32.v:1492](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1492) `mem_do_rinst <= !decoder_trigger && !do_waitirq;`——每个 fetch 态都会请求取下一条指令。load/store 请求则在专门的访存状态里通过「置位脉冲」发起：store 在 [picorv32.v:1870](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1870) 的 `set_mem_do_wdata = 1`，load 在 [picorv32.v:1898](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1898) 的 `set_mem_do_rdata = 1`，最终在 [picorv32.v:1958-1961](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1958-L1961) 把它们写入 `mem_do_rdata`/`mem_do_wdata`。事务完成后的统一清零在 [picorv32.v:1949-1953](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1949-L1953)：`if (!resetn || mem_done) begin mem_do_prefetch<=0; mem_do_rinst<=0; mem_do_rdata<=0; mem_do_wdata<=0; end`。

现在看 `mem_state` 状态机本体 [picorv32.v:565-641](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L565-L641)。复位块 [picorv32.v:566-572](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L566-L572) 把 `mem_state<=0`、撤掉 `mem_valid`。关键是「把 Look-Ahead 信息寄存成对外信号」这一段 [picorv32.v:574-580](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L574-L580)：

```verilog
if (mem_la_read || mem_la_write) begin
    mem_addr  <= mem_la_addr;
    mem_wstrb <= mem_la_wstrb & {4{mem_la_write}};   // 读事务时屏蔽为 0
end
if (mem_la_write) begin
    mem_wdata <= mem_la_wdata;
end
```

注意 `& {4{mem_la_write}}` 这个小技巧：写事务时 `mem_la_write=1`，掩码原样透传；读事务时 `mem_la_write=0`，掩码被强制清成 `0000`——这正是「读时 `mem_wstrb` 必为 0」的硬保证。这也解释了为什么对端可以直接用 `mem_wstrb==0` 判断「这是读」。

四态的 `case` 在 [picorv32.v:581-636](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L581-L636)，逐个看：

- **状态 0** [picorv32.v:582-594](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L582-L594)：有读请求时 `mem_valid<=1; mem_instr<=(prefetch||rinst); mem_wstrb<=0; mem_state<=1`；有写请求时 `mem_valid<=1; mem_instr<=0; mem_state<=2`。
- **状态 1**（读在途）[picorv32.v:595-620](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L595-L620)：等到 `mem_xfer`，撤掉 `mem_valid`，并按「是否还需要消费」分流：`mem_state <= mem_do_rinst || mem_do_rdata ? 0 : 3`（压缩指令集的「第二个半字」分支 `mem_la_secondword` 也在这里处理，留给 [u7-l2](u7-l2-compressed-isa.md)）。
- **状态 2**（写在途）[picorv32.v:621-628](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L621-L628)：等到 `mem_xfer`，撤 `mem_valid`，回 `mem_state<=0`。此态下带断言 `assert(mem_wstrb != 0)`，与状态 1 的 `assert(mem_wstrb == 0)` 形成对称校验。
- **状态 3**（预取暂存）[picorv32.v:629-635](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L629-L635)：什么也不做地等，直到 `mem_do_rinst` 为真（主状态机终于要消费这条预取指令）才回状态 0。

读回的数据在 `mem_xfer` 那一拍被锁存进 `mem_rdata_q` 与 `next_insn_opcode`（[picorv32.v:430-434](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L430-L434)），供译码器或 load 路径使用。一个细节：参数 `LATCHED_MEM_RDATA`（[picorv32.v:67](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L67)、[picorv32.v:384](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L384)）控制 `mem_rdata_latched` 是取「成交当拍的 `mem_rdata`」还是「上一拍锁存的 `mem_rdata_q`」——当外存只在 `mem_ready` 拍才给出有效数据时用默认值 0，当外存数据保持多拍有效时可以设 1 以放松时序。

最后，状态机带一组自检断言 [picorv32.v:546-562](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L546-L562)，确保「读/写/预取」四类请求互斥（同一拍至多一类），且状态 2/3 时 `mem_valid` 或 `mem_do_prefetch` 必有一个为真。这些断言是理解「合法状态组合」的最佳速查表。

#### 4.2.4 代码实践

**目标**：用源码阅读的方式，追踪一条 `lw`（load word）指令在 `mem_state` 里的完整流转，并把每个状态对应的主状态机动作对上号。

**步骤**：

1. 在 [picorv32.v:1880-1912](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1880-L1912)（`cpu_state_ldmem`）找到 load 的发起：先按 `instr_lb/lh/lw` 设 `mem_wordsize`，再 `set_mem_do_rdata = 1`。
2. 切换视角到 `mem_state`：`mem_do_rdata=1` → 状态 0 看到「要读」→ [picorv32.v:583-588](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L583-L588) 拉高 `mem_valid`，进入状态 1。
3. 在状态 1 等 `mem_xfer`（[picorv32.v:600](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L600)），成交后 [picorv32.v:617](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L617) 因为 `mem_do_rdata` 为真而回到状态 0。
4. 回到主状态机：`mem_done` 拉高后 [picorv32.v:1900-1910](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1900-L1910) 把 `mem_rdata_word` 写进 `reg_out`，然后 `cpu_state <= cpu_state_fetch`。

**需要观察的现象**：`lw` 的访存阶段在 `mem_state` 上画出 `0→1→0` 的轨迹，且状态 1 持续的拍数 = 外端插入的等待拍数 + 1。若把测试台的 `mem_ready` 改成延迟 2 拍才回，状态 1 会多停 2 拍。

**预期结果**：你能画出一张「`cpu_state_ldmem` 设 `mem_do_rdata` → `mem_state` 0→1→0 → `mem_done` → 数据写回 `reg_out` → 回 `cpu_state_fetch`」的调用链。

> 本实践为纯源码阅读型，不需要运行；若想验证「延迟 ready 会拉长状态 1」，可在 testbench_ez.v 里把 `mem_ready <= 1` 那行改成延迟若干拍（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么状态 1 的断言是 `assert(mem_wstrb == 0)`，而状态 2 是 `assert(mem_wstrb != 0)`？
**答案**：状态 1 是「读在途」，读事务的 `mem_wstrb` 必为 0；状态 2 是「写在途」，写事务的 `mem_wstrb` 必为 8 种非零合法值之一。这组对称断言把「读/写」与「状态」的一致性钉死。

**练习 2**：状态 3 在什么情况下出现？为什么它不直接回 0？
**答案**：状态 3 出现在「预取了一条指令但主状态机这一拍还不想消费它」时。不直接回 0 是为了把这笔预取「挂起」——当 `cpu_state_fetch` 真正请求这条指令（`mem_do_rinst=1`）时，状态 3→0，而已预取的数据在 `mem_rdata_q`/`next_insn_opcode` 里直接可用，免去重新取指。这是用「暂存」换「少一次访存」的小优化。

**练习 3**：`mem_do_rdata` 和 `mem_do_wdata` 能否同一拍都为 1？
**答案**：不能。断言 [picorv32.v:557-558](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L557-L558) 明确要求 `mem_do_wdata` 为真时其余三个请求标志都为 0——接口「一次一笔」，读和写不能并发。

---

### 4.3 Look-Ahead 接口：提前一拍给出事务信息

#### 4.3.1 概念说明

原生接口有一个「先天迟钝」：CPU 要等到 `mem_state` 进入状态 1/2、`mem_valid` 拉高**那一拍**，才把 `mem_addr`/`mem_wdata`/`mem_wstrb` 对外暴露。如果外存（RAM）本身需要一个周期才能读出数据，那么从「CPU 想读」到「数据到手」至少是两拍。

Look-Ahead（前瞻）接口用一组**纯组合输出** `mem_la_read`/`mem_la_write`/`mem_la_addr`/`mem_la_wdata`/`mem_la_wstrb`，在 `mem_valid` 拉高的**前一拍**就把「下一笔事务是什么」全部预告出去。外存可以拿这个「提前量」去预先启动 RAM 读，等到 `mem_valid` 真的拉高时，数据已经备好，事务一拍成交。

这就是 README 里那句「Without using the look-ahead memory interface (usually required for max clock speed), this results drop to 0.305 DMIPS/MHz and 5.232 CPI」（[README.md:372-373](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L372-L373)）的硬件根因——不用 Look-Ahead，每笔访存多一拍，CPI 从 ~4 涨到 ~5.2。

代价写在 README 的 Note 里：`mem_la_read`/`mem_la_write`/`mem_la_addr` 是组合逻辑驱动的，处在更长的关键路径上，**更难过时序收敛**（[README.md:438-441](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L438-L441)）。所以 Look-Ahead 是「拿时序裕量换 CPI」的旋钮：用它跑得快但频率可能略低，不用它频率好做但 CPI 高。

#### 4.3.2 核心流程

Look-Ahead 与普通接口的时间关系（以一次读为例，假设 RAM 读需 1 拍）：

```
普通接口（两拍成交）：
  拍 N-1: CPU 内部算出要读，但 mem_valid 仍为 0
  拍 N  : mem_valid↑, mem_addr 有效            —— RAM 这一拍才开始读
  拍 N+1: RAM 把数据送上 mem_rdata, mem_ready↑  —— 成交（mem_xfer）
  → 读耗时 2 拍

Look-Ahead 接口（单拍成交）：
  拍 N-1: mem_la_read 脉冲, mem_la_addr 已有效  —— RAM 用这个地址预先读
  拍 N  : mem_valid↑, mem_ready↑, mem_rdata 已就绪 —— 成交（mem_xfer）
  → 读耗时 1 拍
```

用公式表达两种接法下单笔读事务的耗时（设外端从拿到地址到给出数据需 \(t_{\text{ram}}\) 拍，\(t_{\text{ram}}\ge 1\)）：

\[
T_{\text{普通}} = 1 + t_{\text{ram}}, \qquad T_{\text{LA}} = \max(1,\, t_{\text{ram}}-1+1) = t_{\text{ram}}
\]

当 \(t_{\text{ram}}=1\)（RAM 一拍即就绪）时，普通接口要 2 拍、Look-Ahead 只要 1 拍，正好差一拍。这一拍乘以每条指令的访存次数，就是 README 里 0.516 → 0.305 DMIPS/MHz 的差距来源。

#### 4.3.3 源码精读

Look-Ahead 的三根「事务预告」信号都是 `assign`（纯组合）：

[picorv32.v:379-382](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L379-L382)

```verilog
assign mem_la_write = resetn && !mem_state && mem_do_wdata;
assign mem_la_read  = resetn && ((!mem_la_use_prefetched_high_word && !mem_state &&
                                  (mem_do_rinst || mem_do_prefetch || mem_do_rdata)) || ...);
assign mem_la_addr  = (mem_do_prefetch || mem_do_rinst) ? {next_pc[31:2] + mem_la_firstword_xfer, 2'b00}
                                                        : {reg_op1[31:2], 2'b00};
```

读这三行要抓住三点：

1. **`!mem_state`**：只在 `mem_state==0`（空闲）时才发预告。因为预告的是「下一笔新事务」，事务已在途（状态 1/2/3）时不能重复预告。
2. **预告的依据是请求标志**：`mem_do_wdata` 为真就预告写，`mem_do_rinst/prefetch/rdata` 任一为真就预告读——和 `mem_state` 状态 0 的分流完全一致，所以「下一拍 `mem_valid` 拉高时干的事」和「这一拍 `mem_la_*` 预告的事」必然对得上。
3. **`mem_la_addr` 直接指出地址**：取指/预取时用 `next_pc`（下一条指令地址，按字对齐 `{... , 2'b00}`），访存时用 `reg_op1`（算好的数据地址）。注意地址是字对齐的，低 2 位为 0——字节选择交给 `mem_la_wstrb`。

`mem_la_wdata`/`mem_la_wstrb` 在前文 4.1.3 的 `case (mem_wordsize)` 里同时生成（[picorv32.v:405-419](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L405-L419)）。

关键在于：这些 Look-Ahead 信号**也是内部 `mem_state` 寄存对外信号的源头**。回看 [picorv32.v:574-580](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L574-L580)：`mem_addr <= mem_la_addr; mem_wstrb <= mem_la_wstrb & {4{mem_la_write}}; mem_wdata <= mem_la_wdata;`。也就是说，同一组组合值，**对外**提前一拍作为 `mem_la_*` 暴露给高性能外存，**对内**在 `mem_la_read/write` 脉冲那一拍被寄存成下一拍的 `mem_addr/mem_wstrb/mem_wdata`。这是 Look-Ahead 不增加额外硬件、只是「把内部已有的组合中间结果也引出来」的精妙之处。

看一个真实使用范例：[dhrystone/testbench.v:62-85](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/dhrystone/testbench.v#L62-L85) 用 Look-Ahead 实现了单拍内存：

```verilog
assign mem_ready = 1;                         // 永远就绪
always @(posedge clk) begin
    mem_rdata[ 7: 0] <= mem_la_read ? memory[mem_la_addr + 0] : 'bx;  // 用前瞻地址预读
    mem_rdata[15: 8] <= mem_la_read ? memory[mem_la_addr + 1] : 'bx;
    mem_rdata[23:16] <= mem_la_read ? memory[mem_la_addr + 2] : 'bx;
    mem_rdata[31:24] <= mem_la_read ? memory[mem_la_addr + 3] : 'bx;
    if (mem_la_write) begin
        ...
        if (mem_la_wstrb[0]) memory[mem_la_addr + 0] <= mem_la_wdata[ 7: 0];
        ...
    end
end
```

读懂这段的关键：`mem_ready` 被绑成常数 1，意味着「只要你 `mem_valid` 一拉高，我当拍就成交」。为了让 `mem_rdata` 在 `mem_valid` 那一拍就有效，测试台**提前一拍**用 `mem_la_addr` 把 RAM 内容读进 `mem_rdata` 寄存器——等下一拍 `mem_valid && mem_ready` 同时为高时，数据刚好就位。写入同理，用 `mem_la_write`/`mem_la_wstrb`/`mem_la_wdata` 提前一拍完成。

> 注意这里 `mem_la_addr` 是字节地址（`+0/+1/+2/+3`），因为它直接喂给按字节组织的 `reg [7:0] memory[]`；而 CPU 对外的 `mem_addr` 低 2 位为 0（字对齐）。这正是 Look-Ahead 接口「更原始、更贴近内部」的体现——它给出了内部真实使用的字节级地址信息。

对照 [testbench_ez.v:72-85](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L72-L85) 的普通接法：那里**没有**用 `mem_la_*`，而是看到 `mem_valid && !mem_ready` 后「下一拍」才回 `mem_ready=1` 并读 RAM——所以每笔事务必花两拍。两份测试台并排看，就是 Look-Ahead 价值的最直观教材。

#### 4.3.4 代码实践

**目标**：亲手写一个把 `picorv32` 接到简单 RAM 的小封装，分别用「普通接口（两拍）」和「Look-Ahead（单拍）」两种接法，对比二者时序。

**步骤**：

1. 先实现「普通接口」版本。下面是示例代码（基于 testbench_ez.v 改写，标注为「示例代码」）：

   ```verilog
   // 示例代码：普通接口（两拍握手）接法
   module picorv32_ram_slow (
       input clk, resetn, output trap
   );
       wire        mem_valid, mem_instr;
       reg         mem_ready;
       wire [31:0] mem_addr, mem_wdata;
       wire [ 3:0] mem_wstrb;
       reg  [31:0] mem_rdata;

       picorv32 cpu (
           .clk(clk), .resetn(resetn), .trap(trap),
           .mem_valid(mem_valid), .mem_instr(mem_instr), .mem_ready(mem_ready),
           .mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_wstrb(mem_wstrb),
           .mem_rdata(mem_rdata)
       );

       reg [31:0] mem [0:1023];

       always @(posedge clk) begin
           mem_ready <= 0;                       // 默认不就绪
           if (mem_valid && !mem_ready) begin    // 看到请求，下一拍才回 ready
               mem_ready <= 1;
               mem_rdata <= mem[mem_addr[31:2]]; // 字地址 = 字节地址>>2
               if (mem_wstrb[0]) mem[mem_addr[31:2]][ 7: 0] <= mem_wdata[ 7: 0];
               if (mem_wstrb[1]) mem[mem_addr[31:2]][15: 8] <= mem_wdata[15: 8];
               if (mem_wstrb[2]) mem[mem_addr[31:2]][23:16] <= mem_wdata[23:16];
               if (mem_wstrb[3]) mem[mem_addr[31:2]][31:24] <= mem_wdata[31:24];
           end
       end
   endmodule
   ```

2. 再实现「Look-Ahead 单拍」版本（仿照 dhrystone/testbench.v）：

   ```verilog
   // 示例代码：Look-Ahead 接口（单拍握手）接法
   module picorv32_ram_fast (
       input clk, resetn, output trap
   );
       wire        mem_valid, mem_instr;
       wire [31:0] mem_addr, mem_wdata;
       wire [ 3:0] mem_wstrb;
       reg  [31:0] mem_rdata;
       wire        mem_la_read, mem_la_write;
       wire [31:0] mem_la_addr, mem_la_wdata;
       wire [ 3:0] mem_la_wstrb;

       picorv32 cpu (
           .clk(clk), .resetn(resetn), .trap(trap),
           .mem_valid(mem_valid), .mem_instr(mem_instr), .mem_ready(1'b1),  // 常数就绪
           .mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_wstrb(mem_wstrb),
           .mem_rdata(mem_rdata),
           .mem_la_read(mem_la_read), .mem_la_write(mem_la_write),
           .mem_la_addr(mem_la_addr), .mem_la_wdata(mem_la_wdata), .mem_la_wstrb(mem_la_wstrb)
       );

       reg [31:0] mem [0:1023];

       always @(posedge clk) begin
           // 用前瞻地址提前一拍读出，使 mem_rdata 在 mem_valid 当拍就有效
           mem_rdata <= mem_la_read ? mem[mem_la_addr[31:2]] : 32'bx;
           if (mem_la_write) begin
               if (mem_la_wstrb[0]) mem[mem_la_addr[31:2]][ 7: 0] <= mem_la_wdata[ 7: 0];
               if (mem_la_wstrb[1]) mem[mem_la_addr[31:2]][15: 8] <= mem_la_wdata[15: 8];
               if (mem_la_wstrb[2]) mem[mem_la_addr[31:2]][23:16] <= mem_la_wdata[23:16];
               if (mem_la_wstrb[3]) mem[mem_la_addr[31:2]][31:24] <= mem_la_wdata[31:24];
           end
       end
   endmodule
   ```

3. 给两个封装各套一个最小测试台（`always #5 clk=~clk`、复位 100 拍后释放、跑若干千拍），用 `$display` 在 `mem_valid && mem_ready` 拍打印事务，对比两类核完成同一小循环所需的总周期数。

**需要观察的现象**：

- 普通版里，`mem_ready` 是寄存器，从看到 `mem_valid` 到回 `mem_ready` 中间隔一拍，每笔事务占 2 拍。
- Look-Ahead 版里，`mem_ready` 恒为 1，`mem_rdata` 用 `mem_la_addr` 预读，每笔事务占 1 拍；完成同一循环的总周期数明显更少。
- 在波形上能看到 `mem_la_read` 比 `mem_valid` 提前一拍出现，且 `mem_la_addr` 在那一拍已经指向正确地址。

**预期结果**：对于一段「取指—执行—写回」的小循环，Look-Ahead 版的总周期数约为普通版的 0.7~0.8 倍（视指令混合而定）；这与 README 给出的 CPI 4.1（用 LA）vs 5.2（不用 LA）比例相符。

> 本实践需要 iverilog 或其它仿真器；若本地没有，可降级为「源码阅读型」：对照 testbench_ez.v 与 dhrystone/testbench.v，在纸上画出两种接法的逐拍时序波形（待本地验证运行结果）。

最后回答实践任务里「意义与代价」的问题：

- **意义**：Look-Ahead 让外存可以提前一拍启动读/写，把「CPU 给地址 → 数据到手」的窗口压缩一拍，从而把每条指令的平均 CPI 从 ~5.2 降到 ~4.1（README 实测），是榨取性能的关键。
- **代价**：`mem_la_read`/`mem_la_write`/`mem_la_addr` 是纯组合输出，挂在从请求标志、`next_pc`/`reg_op1` 直到端口的长组合路径上；这条路径更难在目标频率内收敛，可能迫使整体 fmax 下降。是否启用，是「更低 CPI」与「更高 fmax」之间的取舍——这也是为什么 README 说 Look-Ahead「usually required for max clock speed」（在 CPI 层面），但又提醒它「harder to achieve timing closure」（在频率层面）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Look-Ahead 版可以把 `mem_ready` 绑成常数 1，而普通版不能？
**答案**：Look-Ahead 版用 `mem_la_addr` 提前一拍把 `mem_rdata` 准备好，所以 `mem_valid` 拉高当拍数据已就绪，`mem_ready=1` 立即成交合法。普通版若也把 `mem_ready` 绑 1，则 `mem_valid` 拉高当拍 RAM 还来不及给出数据（地址同拍才有效），`mem_rdata` 无效，CPU 会读到错值——所以普通版必须延迟一拍再回 `mem_ready`。

**练习 2**：`mem_la_addr` 与对外的 `mem_addr` 有何不同？
**答案**：两者数值同源（取指用 `next_pc`、访存用 `reg_op1`），但 `mem_la_addr` **早一拍**出现，且它是组合输出（无寄存器），低 2 位的处理更贴近内部真实使用（dhrystone 测试台里直接当字节地址 `+0..+3` 用）；而 `mem_addr` 是寄存器输出、低 2 位恒为 0（字对齐），由 `mem_la_addr` 在 `mem_la_read/write` 脉冲拍寄存而来。

**练习 3**：如果一块 FPGA 的 Block RAM 读端口本身就是「寄存器输出、地址给一拍后数据才出」的同步 RAM，用哪种接口更自然？
**答案**：用 Look-Ahead 接口更自然——同步 RAM 正好需要「这一拍给地址、下一拍出数据」，`mem_la_addr` 提前一拍给出地址正好对上同步 RAM 的时序，配合 `mem_ready=1` 可实现单拍成交。这也是为什么追求性能的 FPGA 设计几乎都用 Look-Ahead。

---

## 5. 综合实践

把本讲三块内容串起来的综合任务：**为 PicoRV32 写一个带「可配置等待周期」的内存控制器，并用它定量测量等待周期对 CPI 的影响。**

要求：

1. 写一个 `picorv32_mem` 模块，参数化一个 `WAIT_CYCLES`（0 表示用 Look-Ahead 单拍，≥1 表示普通接口插入的等待拍数）。当 `WAIT_CYCLES=0` 时用 `mem_la_*` 预读、`mem_ready=1`；当 `WAIT_CYCLES≥1` 时用普通 `mem_*` 接口，并用一个计数器在 `mem_valid` 后数到 `WAIT_CYCLES` 才拉高 `mem_ready`。
2. 复用 [testbench_ez.v:63-70](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L63-L70) 里那段 6 条指令的自增循环作为测试程序（`li/sw/lw/addi/sw/j`）。
3. 用一个计数器统计「完成 N 次 `write 0x3fc`（即 N 次循环）所花的总时钟周期数」，分别取 `WAIT_CYCLES = 0、1、2` 跑三次。
4. 把结果填入下表并解释：

| `WAIT_CYCLES` | 接口类型 | 单笔访存拍数 | 完成一次循环的总周期 | 相对 0 的比值 |
| --- | --- | --- | --- | --- |
| 0 | Look-Ahead | 1 | ? | 1.0 |
| 1 | 普通（最小等待） | 2 | ? | ? |
| 2 | 普通（多一等） | 3 | ? | ? |

**预期结论**：`WAIT_CYCLES` 每增加 1，循环总周期大约按「循环里访存笔数 × 1 拍」线性增加；这与本讲 4.3.2 的公式 \(T=1+t_{\text{ram}}\) 一致，也定性复现了 README 里「不用 Look-Ahead → CPI 从 4.1 涨到 5.2」的现象。

> 本任务依赖 iverilog；若无法运行，请至少完成设计、写出 `picorv32_mem` 的 Verilog 并在注释里说明每种 `WAIT_CYCLES` 下 `mem_valid`/`mem_ready`/`mem_rdata` 的预期波形（待本地验证数值）。

## 6. 本讲小结

- PicoRV32 的原生内存接口是**一次一笔**的 valid-ready 接口：`mem_valid && mem_ready` 同拍为高即成交（`mem_xfer`），成交前 CPU 输出保持稳定。
- **读用 `mem_wstrb=0`、写用 `mem_wstrb≠0`**；`mem_wstrb` 是 4 位字节写使能，只有 `0000/1111/1100/0011/1000/0100/0010/0001` 这 8 种合法值，分别对应不写/写字/写高半字/写低半字/写单字节。`mem_instr` 一位区分取指与访存。
- 内部 **`mem_state` 四状态机**（0 空闲 / 1 读在途 / 2 写在途 / 3 预取暂存）把主状态机的「请求标志」`mem_do_prefetch/rinst/rdata/wdata` 翻译成对 `mem_valid` 的逐拍驱动：读走 `0→1→(3|0)`，写走 `0→2→0`。
- Look-Ahead 接口 `mem_la_*` 是**纯组合输出**，在 `mem_valid` 前一拍预告下一笔事务的地址/数据/掩码；它同时也是内部寄存 `mem_addr/mem_wdata/mem_wstrb` 的源头，所以「提前一拍」几乎不增加硬件。
- 用 Look-Ahead 可把单笔访存从 2 拍压到 1 拍，是 CPI ~4.1 vs ~5.2（README 实测）差距的根因；代价是组合路径变长、时序收敛更难。
- 两种接法各有标准范例：普通接口见 [testbench_ez.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v)，Look-Ahead 见 [dhrystone/testbench.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/dhrystone/testbench.v)。

## 7. 下一步学习建议

本讲把「原生内存接口」讲透了，但 PicoRV32 还提供两种**总线变体**，把这套原生接口桥接到工业标准总线上：

- 下一讲 [u7-l1 AXI4-Lite 与 Wishbone 适配](u7-l1-axi-wishbone-adapters.md) 会讲解 `picorv32_axi_adapter` 如何把本讲的 `mem_*` 接口桥接到 AXI4-Lite 的 AW/W/B/AR/R 五通道，以及 `picorv32_wb` 如何适配 Wishbone B4。学完那一讲，你就能把 PicoRV32 接到绝大多数现成的 SoC 总线上。
- 在那之前，建议先回头对照 [u3-l2 端口与四大接口](u3-l2-ports-and-interfaces.md) 里的接口方框图，确认你已经能把本讲的 `mem_*` 与 `mem_la_*` 准确归位到「原生内存接口」和「Look-Ahead 接口」这两组里。
- 如果你对压缩指令集（C 扩展）如何复用本讲接口感兴趣（状态 1 里的 `mem_la_secondword`、`mem_16bit_buffer`、`prefetched_high_word`），可以预习 [u7-l2 RISC-V 压缩指令集支持](u7-l2-compressed-isa.md)，它会展开本讲里刻意略过的「第二个半字」分支。
