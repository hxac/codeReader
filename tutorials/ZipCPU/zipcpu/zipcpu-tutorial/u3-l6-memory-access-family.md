# 访存模块族：memops/pipemem/dcache

## 1. 本讲目标

本讲是「CPU 核心与流水线实现」单元的第六讲，聚焦 ZipCPU 第 4 级（执行级）里**数据访存**这一段。

学完本讲，你应该能够：

- 说出 `memops`、`pipemem`、`dcache` 三个访存模块各自解决什么问题、有什么本质区别。
- 解释 `pipemem` 为什么能把连续访存「压」到接近每条 1 拍，省下的时钟到底从哪儿来。
- 理解 `dcache` 的命中（hit）/缺失（miss）/整行填充（line fill）逻辑，以及它何时零总线访问。
- 根据 `OPT_LGDCACHE`、`OPT_PIPELINED_BUS_ACCESS`、`OPT_DCACHE` 这一组综合期参数，为一个具体设计选出合适的访存模块。

本讲承接 u3-l1（zipcore 五级流水线总览）。回忆一个关键事实：在 u3-l1 中我们看到，**取指缓存与访存控制器都不在 `zipcore` 内核里**，内核只实例化 `idecode`/`cpuops`/`div` 三个子模块，访存通过端口接在外壳（`zipwb.v`）里。本讲要讲的这三个模块，正是挂在这个「外壳」与 Wishbone 总线之间的数据侧访存控制器。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（前几讲已建立）：

- **load/store 架构**：只有 `LW/SW/LH/SH/LB/SB` 这 6 条指令会访问内存（见 u2-l3）。
- **Wishbone B4 流水线总线**：主设备用 `cyc`（周期）/`stb`（选通）/`we`（写使能）/`addr`/`data`/`sel`（字节使能）发起请求，从设备用 `ack`（应答）/`stall`（反压）/`err`（错误）/`data` 回应。`cyc` 为 1 表示一次总线周期尚未结束，期间可以连续发 `stb`，多个 `stb` 与多个 `ack` 可以在时间上重叠（流水线特性）。
- **在途请求（outstanding requests）**：已经发出 `stb` 但还没收到对应 `ack` 的请求数量。Wishbone 流水线允许在途请求数大于 1，这是 `pipemem` 提速的根本来源。
- **综合期参数**：`OPT_*` 参数不是运行时开关，而是综合期的「剪刀」。关闭某个 `OPT_*`，对应电路根本不会被生成（见 u3-l1）。
- **本地总线（local bus）**：ZipSystem 把 `0xFFxxxxxx` 段留作片上外设，访存模块会把目标地址高字节为 `0xFF` 的访问走「本地总线」出口，其余走「全局总线」出口。本讲会反复看到 `lcl_stb`/`gbl_stb`、`o_wb_cyc_lcl`/`o_wb_cyc_gbl` 这两组信号。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [rtl/core/memops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v) | 单次（非流水线）访存控制器：一次只处理一笔总线交易，最多 1 个在途请求。 |
| [rtl/core/pipemem.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pipemem.v) | 流水线访存控制器：保持 `cyc` 不掉，允许最多 `OPT_MAXDEPTH` 个在途请求，把连续访存压到约 1 拍/条。 |
| [rtl/core/dcache.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v) | 带数据缓存的访存控制器：命中走缓存（零/极低延迟），缺失整行突发填充。 |
| [rtl/core/zipwb.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v) | Wishbone 外壳：用一个三选一的 `generate if` 在上述三者之间挑一个实例化为 `mem`。本讲的「选型逻辑」全部集中在这里。 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | 规范：定义了 pipelined memory access 的三项前提与 `OPT_LGDCACHE` 参数含义。 |
| [bench/cpp/dcache_tb.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/cpp/dcache_tb.cpp) | 一个专门验证 `dcache` 行为的独立测试台，本讲实践会用到它。 |

> 说明：这三个访存模块（以及取指模块族）都不在 `zipcore.v` 内部。它们被实例化在 Wishbone 外壳 `zipwb.v` 中；AXI 侧的对应物（`axilops`/`axilpipe`/`axidcache` 等）在 `rtl/core` 里另成一套，本讲只讲 Wishbone 版，AXI 版的对应关系留到 u4-l3。

## 4. 核心概念与源码讲解

### 4.1 访存模块族总览：共同接口与「三选一」逻辑

#### 4.1.1 概念说明

回顾 u3-l1：`zipcore` 内核通过一组 `mem_*` 端口（`o_mem_ce`、`o_mem_op`、`o_mem_addr`、`i_mem_valid`、`i_mem_busy`……）把「我想做一次访存」的意图交给外壳，自己**不关心**总线协议，也不关心有没有缓存。外壳的任务就是：接住这组意图，把它翻译成 Wishbone 交易。

但「翻译成 Wishbone 交易」这件事，可以做得简单，也可以做得复杂：

- **最简单**：来一条访存指令，就发起一笔 Wishbone 交易，等 `ack` 回来再放下一条。代码短、面积小，但连续访存时每条都要等完整往返，慢。
- **进阶**：把连续的访存合并成一次长 Wishbone 周期，请求和应答流水起来，多条访存重叠执行。
- **再进阶**：在中间加一层缓存，命中时根本不上总线。

ZipCPU 把这三种实现都写好了，分别叫 `memops`、`pipemem`、`dcache`，并让它们**对外接口形状几乎一致**（CPU 侧端口、Wishbone 侧端口同名同义），从而可以在外壳里用 `generate if` 三选一，对内核完全透明。这正是「模块族」的含义——和取指模块族（`prefetch`/`dblfetch`/`pfcache`，见 u3-l2）是同一种设计手法。

#### 4.1.2 核心流程

外壳的选择是一个有优先级的 `generate if` 链：

```text
if (OPT_DCACHE)              // 缓存打开
    实例化 dcache
else if (OPT_MEMPIPE)        // 流水线总线访问打开
    实例化 pipemem
else                         // 都关
    实例化 memops
```

其中三个开关都是**从 `OPT_LGDCACHE` 与 `OPT_PIPELINED` 派生出来的综合期常量**：

```text
OPT_DCACHE               = (OPT_LGDCACHE > 2)        // 缓存对数大小 > 2 才算有缓存
OPT_PIPELINED_BUS_ACCESS = (OPT_PIPELINED)
OPT_MEMPIPE              = OPT_PIPELINED_BUS_ACCESS
```

注意优先级：**只要 `OPT_LGDCACHE > 2`，无论是否流水线，都用 `dcache`**（`dcache` 内部还有一个自己的 `OPT_PIPE` 参数控制它自己要不要流水化总线访问）；缓存关掉之后，才轮到 `pipemem`；两者都关，才退回最朴素的 `memops`。

#### 4.1.3 源码精读

派生关系定义在 `zipwb.v` 开头：

[rtl/core/zipwb.v:175-177](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L175-L177) —— 用三行 localparam 把 `OPT_DCACHE`、`OPT_PIPELINED_BUS_ACCESS`、`OPT_MEMPIPE` 从顶层参数 `OPT_LGDCACHE`、`OPT_PIPELINED` 推导出来。

真正的三选一实例化在这里，整段是一个 `generate if ... else if ... else ...`：

[rtl/core/zipwb.v:461-479](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L461-L479) —— `if (OPT_DCACHE)` 分支，实例化 `dcache mem(...)`。注意它给 `dcache` 传了 `.OPT_PIPE(OPT_MEMPIPE)`：缓存是否流水化总线访问，由这个派生参数决定。

[rtl/core/zipwb.v:500-515](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L500-L515) —— `else if (OPT_MEMPIPE)` 分支，实例化 `pipemem domem(...)`。

[rtl/core/zipwb.v:537-548](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L537-L548) —— 兜底分支，实例化 `memops mem(...)`。

三个分支实例的端口连接几乎逐行相同（都连到同一组 `mem_ce`/`mem_op`/`mem_busy`/`mem_valid`/`mem_cyc_gbl`/…… 信号），所以对内核而言「换了访存模块」是感觉不到的。这正是模块族能三选一的前提。

规范侧的权威定义在 spec.tex：`OPT_LGDCACHE` 为 0 → 无缓存的基本控制器；流水线打开时可用允许「多个请求同时在途」的流水线控制器；大于 2 才是真正的数据缓存：

[doc/src/spec.tex:3384-3389](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3384-L3389) —— `OPT_LGDCACHE` 参数语义（日志化的数据缓存大小，>2 才算缓存）。

#### 4.1.4 代码实践：让外壳「换档」

**实践目标**：亲手验证「`OPT_LGDCACHE` 取不同值会实例化不同的访存模块」。

**操作步骤**：

1. 打开 `rtl/core/zipwb.v`，定位到 4.1.3 引用的 `generate if`（约第 461 行）。
2. 在脑中（或在纸上）代入两组参数，预测会被综合出哪个分支：
   - 情形 A：`OPT_PIPELINED = 1, OPT_LGDCACHE = 10`（默认值）→ `OPT_DCACHE = ?`、选哪个模块？
   - 情形 B：`OPT_PIPELINED = 1, OPT_LGDCACHE = 0` → 选哪个模块？
   - 情形 C：`OPT_PIPELINED = 0, OPT_LGDCACHE = 0` → 选哪个模块？
3. 若想真实验证，可用 Verilator 分别以上述参数编译（`zipwb` 自身可被 `bench/cpp` 的测试台引用），用 `grep` 在综合后的层级里查找 `mem`（DATA_CACHE/PIPELINED_MEM/BARE_MEM 三个 generate 块名之一）是否存在。

**预期结果**：

- A：`OPT_DCACHE = (10>2) = 1` → `dcache`。
- B：`OPT_DCACHE = 0`，`OPT_MEMPIPE = 1` → `pipemem`。
- C：两者皆 0 → `memops`。

默认配置（`OPT_LGDCACHE=10`）下，Wishbone 版 ZipCPU 用的是 **`dcache`**。

#### 4.1.5 小练习与答案

**练习 1**：如果设计师想要「最小面积、不要缓存、但保留流水线总线能力」，该设哪两个参数？
**答案**：`OPT_LGDCACHE <= 2`（关缓存，落到 `OPT_MEMPIPE` 分支）且 `OPT_PIPELINED = 1`（开 `OPT_MEMPIPE`），于是选中 `pipemem`。

**练习 2**：为什么 `dcache` 的优先级高于 `pipemem`，而不是反过来？
**答案**：因为 `dcache` 是「`pipemem` 能力 + 缓存」的超集——它内部同样可以流水化总线访问（由 `OPT_PIPE` 控制），还额外提供命中加速。一旦你愿意付出缓存面积，就没有理由再退回不带缓存的 `pipemem`；只有在关掉缓存后才需要在「流水 / 不流水」之间二选一。

---

### 4.2 memops：单次访存状态机

#### 4.2.1 概念说明

`memops` 是三兄弟里最简单的一个。它的设计哲学写在文件头注释里：为了代码简洁，**它一次只接受一条访存命令，在上一条完成前再来新命令，结果不可预测**（"susceptible to unknown results should a new command be sent to it before it completes the last one"）。换句话说，它假定上游（内核）会在它 `o_busy` 期间乖乖停住，不再发新请求。

这对应非流水线（`OPT_PIPELINED = 0`）的 CPU 配置，或者你只想要最朴素访存控制器的场景。它的关键特征是：**最多 1 个在途请求**。

#### 4.2.2 核心流程

`memops` 的状态机极小，核心就两个寄存器 `r_wb_cyc_gbl`/`r_wb_cyc_lcl`：

```text
空闲：
  if (i_stb 且 地址合法):
      根据地址高字节决定走全局还是本地总线
      拉高对应的 cyc 与 stb，送出 we/addr/data/sel
忙（cyc 为 1）：
  if (i_wb_ack 或 i_wb_err):
      拉低 cyc/stb，回到空闲
      若是 ack 且是读，下一拍产出 o_valid 与读回数据
      若是 err，产出 o_err
```

一条访存指令 = 一笔完整 Wishbone 交易（`cyc` 起，`ack`/`err` 终）。两条访存指令之间，`cyc` 必然掉到 0 再重新拉起，中间留有间隙——这就是它慢的根源。

对齐检查也在这里：字/半字访问必须对齐，否则不发起总线交易，直接报 `o_err`。

#### 4.2.3 源码精读

模块端口与参数：

[rtl/core/memops.v:48-96](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L48-L96) —— CPU 侧输入 `i_stb`/`i_op`/`i_addr`/`i_data`/`i_oreg`，输出 `o_busy`/`o_valid`/`o_err`/`o_wreg`/`o_result`；Wishbone 侧输出 `o_wb_cyc_gbl`/`o_wb_cyc_lcl`/`o_wb_stb_*`/`o_wb_we`/`o_wb_addr`/`o_wb_data`/`o_wb_sel`，输入 `i_wb_stall`/`i_wb_ack`/`i_wb_err`/`i_wb_data`。注意 `i_op` 是 3 位，低位决定读/写，高两位决定字节/半字/字宽度。

对齐错误检测：

[rtl/core/memops.v:120-139](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L120-L139) —— 字访问要求字对齐（`i_addr[1:0]` 为 00），半字访问要求半字对齐（`i_addr[0]` 为 0），字节访问永不对齐错。命中即置 `misaligned`。

全局/本地分流：

[rtl/core/memops.v:141-146](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L141-L146) —— `lcl_bus = (地址高字节 == 0xFF)`，本地与全局的 `stb` 互斥，且都要求非 `misaligned`。

核心交易状态机：

[rtl/core/memops.v:148-169](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L148-L169) —— 空闲时收到 `i_stb` 就拉起 `r_wb_cyc_lcl` 或 `r_wb_cyc_gbl`；忙时只要来 `i_wb_ack` 或 `i_wb_err` 就清零。这一段就是「一笔交易从发起到结束」的全部控制。

`o_busy` 的定义极其简洁：

[rtl/core/memops.v:341](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L341) —— `o_busy = (r_wb_cyc_gbl)||(r_wb_cyc_lcl)`。即「只要总线上还有一笔交易没结束，就忙」。`o_busy` 反馈给内核，让流水线停住、不发新请求，正好兑现 4.2.1 提到的假设。

读有效信号：

[rtl/core/memops.v:316-325](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L316-L325) —— 只有「`cyc` 期间收到 `ack` 且本次是读（`!o_wb_we`）」才产生 `o_valid`，把 Wishbone 回来的数据锁存为 `o_result`，并附上 `o_wreg`（告诉写回级写回哪个寄存器）。

「最多 1 个在途请求」的硬保证来自形式化属性段（关于形式化验证本身，见 u5-l2）：

[rtl/core/memops.v:603-621](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L603-L621) —— 断言 `f_outstanding` 在有 `stb` 时必为 0、无 `stb` 时至多为 1，从数学上锁死「单笔在途」这一性质。

#### 4.2.4 代码实践：阅读一段「读」的波形

**实践目标**：在脑中画出 `memops` 处理一次 `LW` 的 Wishbone 波形。

**操作步骤**：

1. 假设片上 RAM 0 等待（`ack` 在 `stb` 后 1 拍返回）。
2. 按 4.2.3 引用的代码段，逐拍推演 `i_stb`、`r_wb_cyc_gbl`、`o_wb_stb_gbl`、`i_wb_ack`、`o_busy`、`o_valid`、`o_result` 这几个信号的变化。
3. 关键观察：在 `o_busy` 为 1 的所有拍里，`i_stb` 必须保持为 0（否则违反模块假设）。

**需要观察的现象**：`cyc` 像一个矩形脉冲——`stb` 拉起的同一拍（或下一拍）拉起 `cyc`，`ack` 来后的下一拍 `cyc`、`stb` 同时落下，再下一拍才允许下一次 `stb`。两次访问之间存在 `cyc=0` 的「空拍」。

**预期结果**：单次读约需 2–3 拍，且**完全不能重叠**。若连发 4 次 `LW`，内核会被 `o_busy` 强制停顿，4 次访问串行排队。具体拍数「待本地验证」（取决于目标 RAM 的等待周期）。

#### 4.2.5 小练习与答案

**练习 1**：`memops` 在 `i_wb_ack` 那一拍之后，`o_valid` 会立刻随数据出现吗？为什么读结果还要再锁存一拍？
**答案**：`o_valid` 在 `ack` 后的下一拍由寄存器输出（见 `always @(posedge i_clk)` 块），`o_result` 同样是寄存器输出。这是因为 `i_wb_data` 是组合到达的应答数据，模块把它与时钟对齐后再交给写回级，保证写回级拿到的是稳定的寄存器值。

**练习 2**：为什么模块头注释强调「上条没完成就来新命令结果不可预测」，而形式化属性段却又能证明它正确？
**答案**：形式化模型在验证时**假设**了上游满足契约（不会在 `o_busy` 时发新 `i_stb`，见 `memops.v` 末尾 Verilator 断言 `assert(!i_stb)` 当 `cyc` 为真）。在这个假设下模块行为被证明正确；契约被破坏时的「不可预测」不在证明范围内，而是通过 `o_busy` 反压由内核负责避免。

---

### 4.3 pipemem：流水线批量访存

#### 4.3.1 概念说明

`pipemem` 的目标写在它自己的文件头：**每个时钟发起一笔流水线 Wishbone 访问，并在存储足够快时每个时钟读回一笔**，从而让片上存储具备「单周期（流水线）访问」的能力。

要做到这一点，它必须打破 `memops` 的两条限制：

1. 不再要求「上条完成才能发下条」——允许**多个请求同时在途**。
2. 不再每笔交易都把 `cyc` 拉低再拉高——在一次突发的连续访问期间**保持 `cyc` 常高**。

但 Wishbone 流水线访问有严格前提，spec.tex 明确列出了三条（spec 把这种模式称为 "pipelined memory access"）。

#### 4.3.2 核心流程

`pipemem` 用一个**小 FIFO** 来管理在途请求。每发一笔 `stb`，就把这笔请求的元信息（写回哪个寄存器、操作宽度、地址低位）压进 FIFO；每收到一个 `ack`，FIFO 读指针前进一格，把对应的 `o_wreg`/`o_result` 交还写回级。这样请求与应答就能在时间上错位重叠。

```text
连续访问突发：
  拉起 cyc，并保持
  每拍（若上游有 i_pipe_stb 且 FIFO 未满 且 总线不 stall）：
      发一笔 stb（addr/data/sel）
      wraddr++，把请求元信息压入 fifo_mem
      在途请求计数 fifo_fill++
  每拍（若 i_wb_ack）：
      读出 fifo_mem[rdaddr]，组装 o_wreg/o_result
      rdaddr++，fifo_fill--
  当 最后一笔 ack 已回 且 上游不再有新请求：
      拉低 cyc，结束本次突发
```

关键能力来自两点：**(a) FIFO 让多笔在途请求的「回寄存器号」能对上号； (b) `o_pipe_stalled` 信号告诉内核「我暂时吃不下了，请别再发」**，从而把背压优雅地传回去。

FIFO 深度由 `OPT_MAXDEPTH` 控制（外壳传入，形式化模式下为 3，描述见 spec 的「多个请求同时在途」）。

#### 4.3.3 源码精读

模块端口（CPU 侧多了 `i_pipe_stb` 与 `o_pipe_stalled` 这对流水线握手）：

[rtl/core/pipemem.v:45-90](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pipemem.v#L45-L90) —— 注意 `OPT_MAXDEPTH` 参数（默认 `4'hd`，即 13）限定了 FIFO 容量，也即最大在途请求数。

请求元信息 FIFO——这是 `pipemem` 的心脏：

[rtl/core/pipemem.v:142-179](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pipemem.v#L142-L179) —— `fifo_mem[wraddr]` 在每笔 `i_pipe_stb` 时存下 `{ i_oreg, i_op宽度, i_addr低位 }`；`wraddr` 随 `i_pipe_stb` 自增、`rdaddr` 随 `i_wb_ack` 自增；`fifo_fill = wraddr - rdaddr` 就是当前在途请求数。

FIFO 满判定与背压：

[rtl/core/pipemem.v:181-206](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pipemem.v#L181-L206) —— `fifo_full` 在 `fifo_fill` 达到 `OPT_MAXDEPTH-1` 时置位，阻止继续收新请求。

`cyc` 保持与结束条件——这是「合并成一次长周期」的关键：

[rtl/core/pipemem.v:218-270](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pipemem.v#L218-L270) —— 进入新访问时拉起 `cyc`/`stb`；只有当「最后一个在途请求的 ack 已回（`nxt_rdaddr == wraddr`）且 没有新请求到来」或总线错误时，才拉低 `cyc`。也就是说，只要上游连绵不断地发、且应答没全部回来，`cyc` 就一直高。

背压信号 `o_pipe_stalled`：

[rtl/core/pipemem.v:451-455](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pipemem.v#L451-L455) —— `o_pipe_stalled = (cyc && fifo_full) || (cyc && (i_wb_stall || 没有有效 stb))`。它把「FIFO 满」与「从设备反压」两种情况合并，告诉内核暂停发射。

读有效与结果组装：

[rtl/core/pipemem.v:379-409](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/pipemem.v#L379-L409) —— `o_valid = (cyc)&&(i_wb_ack)&&(!o_wb_we)`；`o_wreg` 从 `fifo_mem[rdaddr]` 取出，确保多个在途读结果按顺序、对号入座地写回各自的目的寄存器。

spec 对「pipelined memory access」三项前提的权威描述：

[doc/src/spec.tex:1770-1784](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1770-L1784) —— 突发内必须全是读或全是写、必须用同一个基址寄存器、突发内不得有停顿或其它指令。典型场景是栈的压入/弹出（连续 `SW`/`LW`）。

#### 4.3.4 代码实践：观察「流水线」时序

**实践目标**：对比 `pipemem` 与 `memops` 在连续 4 笔读上的时序差异。

**操作步骤**：

1. 用 `bench/cpp` 或 `sim/verilator` 以「关缓存 + 开流水线」（即 `OPT_LGDCACHE <= 2`、`OPT_PIPELINED = 1`，对应选中 `pipemem`）的配置编译一个测试程序。
2. 写一段连续 4 笔 `LW`（用同一基址寄存器、连续地址、中间不夹其它指令），满足 spec 三项前提：

   ```asm
   ; 以下为示意汇编（偏移量的具体字节编码请以 u2-l3 讲义与 spec 为准）
   LW   R1, (R2)        ; 第 1 笔
   LW   R3, +4(R2)      ; 第 2 笔
   LW   R4, +8(R2)      ; 第 3 笔
   LW   R5, +12(R2)     ; 第 4 笔
   ```

3. 用 Verilator 的波形（VCD）观察 `o_wb_cyc_gbl`、`o_wb_stb_gbl`、`i_wb_ack` 三组信号。

**需要观察的现象**：`cyc` 在 4 笔访问期间**始终保持高**；`stb` 连续 4 拍逐笔发出；`ack` 在首笔往返延迟后，以「每拍一个」的速率连续返回。请求数与应答数在时间上明显重叠。

**预期结果**：4 笔读总耗时 ≈ 「首笔往返延迟 + 3 拍」，远少于 `memops` 的「4 × 单笔往返」。省下的时钟来自：(1) 不在访问间反复拉低/拉高 `cyc`；(2) 请求 N+1 在请求 N 的应答回来之前就已发出，多个请求同时在途。

> 提示：若运行环境不便生成波形，可改为纯阅读型实践——对照 4.3.3 的代码段，逐拍推演 `fifo_fill` 随 `i_pipe_stb`/`i_wb_ack` 的增减，说服自己「4 笔可同时存在在途」。具体波形拍数「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`pipemem` 为什么需要一个 FIFO，而 `memops` 不需要？
**答案**：`memops` 最多 1 个在途请求，发出请求与收到应答一一紧邻，直接用一组寄存器保存当前请求即可。`pipemem` 允许多个请求同时在途，第 N 个 `ack` 回来时要知道它对应当初第 N 个请求（写回哪个寄存器、什么宽度），所以必须用 FIFO 把每个请求的元信息按顺序记下，与 `ack` 的到达顺序一一匹配。

**练习 2**：如果突发中第 3 笔 `LW` 的基址寄存器跟前两笔不同（违反 spec 前提 2），会发生什么？
**答案**：会破坏「同一突发」的语义。`pipemem` 的连续 `cyc` 假定地址在同一基址下连续递增；换基址会被视为新的访问模式，内核侧的 `pipemem` 控制通常会在这种边界处停止当前突发、重新建立周期，从而失去流水线加速——这正是 spec 把「同一基址」列为前提的原因。

---

### 4.4 dcache：数据缓存

#### 4.4.1 概念说明

`dcache` 是三兄弟里最强的一个，文件头点明它的设计目标：**作为 `pipemem` 的「直接替换件」（drop-in replacement），目标是让访问「最近用过的缓存行」达到单周期读、访问「已在缓存中」的任意位置达到两周期读**。

它在 `pipemem` 的能力之上，加了一层**数据缓存**：把可缓存（cacheable）地址的数据按「缓存行」缓存起来。于是访问分四种情况：

1. **写**：永远直通总线（写永远是某种意义上的 miss），但若写的地址恰好在缓存行内，会同时更新缓存，保持一致。
2. **读不可缓存地址**：直通总线，不进缓存（典型如 memory-mapped 外设）。
3. **读、地址已在缓存（hit）**：直接从缓存数组读，**零总线访问**。其中命中「最近一行」是单周期捷径（`r_svalid`），命中其它行是两周期路径（`r_dvalid`）。
4. **读、地址可缓存但不在缓存（miss）**：发起一次**整行突发读取**把整条缓存行从总线搬进来，再交给 CPU。

#### 4.4.2 核心流程

`dcache` 用一个四状态机 `state` 管理总线侧：

```text
DC_IDLE  空闲
DC_WRITE 写交易（直通总线，必要时顺带写缓存）
DC_READS 读不可缓存地址（单笔直通总线）
DC_READC 读缺失——整行突发填充缓存行
```

读命中走一条「绕开状态机」的快速路径：

```text
读命中决策（每个 i_pipe_stb 组合判断）：
  addr 是否可缓存？  （iscachable 模块 + 本地总线/锁排除）
  addr 是否在「最近用过的有效行」里？ → 单周期路径 r_svalid（直接读 cached_iword）
  否则 addr 是否在缓存里（tag 命中且行有效）？ → 两周期路径 r_dvalid
  否则 → r_cache_miss，进入 DC_READC 整行填充
```

缓存行结构由两个参数决定：`LGCACHELEN`（缓存总位数的对数）与 `LGNLINES`（缓存行数的对数），由此推出 `LS = LGCACHELEN - LGNLINES`（每行内字数的对数）。一次 miss 就突发读 `1<<LS` 个字。这与取指侧 `pfcache`（见 u3-l2）按整行突发填充是同一种思路。

地址是否「可缓存」由外部模块 `iscachable.v` 判定（地址译码器提供），这是 `dcache` 与外壳之间的一条额外约定（详见 spec「Memory Architecture」节关于 bus compositor 必须提供 `iscachable` 的说明）。

#### 4.4.3 源码精读

模块参数与状态定义：

[rtl/core/dcache.v:87-117](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L87-L117) —— `LGCACHELEN`/`LGNLINES` 决定容量与行长，`LS = CS - LGNLINES` 决定每行字数；四个状态常量 `DC_IDLE/DC_WRITE/DC_READS/DC_READC`。

三组缓存存储结构：

[rtl/core/dcache.v:171-177](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L171-L177) —— `c_v`（每行 1 个有效位）、`c_vtags`（每行的地址 tag）、`c_mem`（实际缓存的数据阵列）。

地址切片与「最近一行」捷径：

[rtl/core/dcache.v:224-238](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L224-L238) —— `i_cline`/`i_caddr` 把请求地址切成「行号 + 行内偏移」；`cache_miss_inow` 用上一次的 `last_tag` 直接判断「是否就在最近这一行」，命中即走单周期路径，避免每笔访问都等 tag 比较的两拍延迟。

可缓存性判定：

[rtl/core/dcache.v:231-243](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L231-L243) —— `w_cachable` 综合了「非本地总线、非 lock、且 `iscachable` 模块判为可缓存」三个条件。`iscachable chkaddress(...)` 这一行就是调用外壳提供的可缓存性判定。

读命中/缺失的寄存器化决策：

[rtl/core/dcache.v:249-331](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L249-L331) —— `r_svalid`（单周期命中）、`r_dvalid`（两周期命中）、`r_cache_miss`（需要整行填充）、`r_rd_pending` 等信号，把「命中还是 miss」这件组合判断寄存器化，驱动后续总线动作。

四状态总线主控状态机（本模块的「BIG STATE MACHINE」）：

[rtl/core/dcache.v:886-1169](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L886-L1169) —— 处理写、不可缓存读、整行填充三类总线交易。其中写分支会判断目标是否在有效行内，若是则同时写缓存（`c_wr`/`c_wdata`/`c_wsel`/`c_waddr`），保持一致性。

整行填充分支 `DC_READC`：

[rtl/core/dcache.v:1040-1079](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L1040-L1079) —— miss 时从行起始地址发起 `1<<LS` 笔连续读，每个 `ack` 把数据写入缓存数组并在 `c_waddr` 自增，行末或错误时回到 `DC_IDLE`、置行有效位。

数据来源选择（缓存 / 总线）：

[rtl/core/dcache.v:1286-1319](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L1286-L1319) —— `o_data` 来自三处之一：`r_svalid` 时取 `cached_iword`、`DC_READS` 时取总线 `i_wb_data`、否则取 `cached_rword`，再按地址低位做字节/半字/字的对齐截取。

`o_valid`（命中即有效，不等总线）：

[rtl/core/dcache.v:1322-1331](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L1322-L1331) —— `state==DC_READS` 时 `o_valid` 随总线 `ack`；否则 `o_valid = r_svalid || r_dvalid`。**命中的读完全不触发总线**，这就是缓存的收益所在。

#### 4.4.4 代码实践：阅读 dcache 专用测试台

**实践目标**：用现成的 `dcache_tb` 理解命中 / 缺失 / 整行填充的实际行为。

**操作步骤**：

1. 打开 [bench/cpp/dcache_tb.cpp:48-55](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/cpp/dcache_tb.cpp#L48-L55)。它实例化了 Verilator 生成的 `Vdcache`，并带一个 `memsim` 模拟从设备。
2. 阅读测试台如何驱动 `i_pipe_stb`/`i_op`/`i_addr` 发起连续读，以及如何检查 `o_valid`/`o_data`。
3. 尝试编译并运行（入口在 `bench/cpp/Makefile`）：构造一次 miss（访问一个新地址行），观察整行突发；紧接着访问**同一行**内的相邻地址，观察这次命中没有总线活动。

**需要观察的现象**：第一次访问触发一串 `o_wb_stb_gbl` + 多个 `i_wb_ack`（整行填充）；第二次访问同行的相邻字时，`o_wb_cyc_gbl` 全程为 0，`o_valid` 几乎立即拉高，数据直接来自缓存。

**预期结果**：同一行内的后续读命中——零总线、1–2 拍返回。具体行为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `dcache` 要区分「最近一行」的单周期命中和其它命中的两周期命中？
**答案**：因为缓存阵列是寄存器化的块 RAM，读出数据需要一拍。要判断「是否命中」要先读出该行的 tag 做比较，又得一拍。为常用情况（顺序访问往往落在刚刚访问过的同一行）开一条「单周期捷径」，用一个 `last_tag` 寄存器记住上一笔命中的 tag，下一笔若仍在同一行就跳过 tag 比较的那一拍，直接给数据。

**练习 2**：写一笔数据到某个地址，该地址恰好在某条有效缓存行内，缓存会怎样？
**答案**：见 `DC_WRITE` 分支：写永远直通总线，**同时**通过 `c_wr`/`c_wdata`/`c_waddr` 把新值写进缓存数组（[dcache.v:1117-1135](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/dcache.v#L1117-L1135)），保证后续读命中能拿到最新值。这是写直达（write-through）策略。

---

### 4.5 三兄弟横向对比与选型速查

把三个模块放在一起对照（这是本讲学习目标「何时用哪个」的速查表）：

| 维度 | memops | pipemem | dcache |
|------|--------|---------|--------|
| 在途请求数 | 最多 1 | 最多 `OPT_MAXDEPTH` | 命中时 0 笔总线在途；miss 时按行长突发 |
| 连续访问能否重叠 | 否（串行） | 是（流水线） | 是（且命中时不上总线） |
| `cyc` 形态 | 每笔一个脉冲 | 突发内常高 | 命中无 `cyc`；miss/写时突发 |
| 缓存 | 无 | 无 | 有（有效位 + tag + 数据阵列） |
| 命中延迟 | —— | —— | 单周期（最近行）/ 两周期（其它行） |
| 适用配置 | `!OPT_PIPELINED` 且 `OPT_LGDCACHE<=2` | `OPT_PIPELINED` 且 `OPT_LGDCACHE<=2` | `OPT_LGDCACHE>2`（默认） |
| 面积 | 最小 | 中 | 最大 |
| CPU 侧握手 | `i_stb`/`o_busy` | `i_pipe_stb`/`o_pipe_stalled` | `i_pipe_stb`/`o_pipe_stalled` |

一句话选型：**面积敏感且非流水 → memops；要吞吐但不要缓存 → pipemem；要最佳访存性能且能承受缓存面积 → dcache（默认）。**

## 5. 综合实践

**任务**：用一段对连续地址的 4 次 `LW`，把 `memops`、`pipemem`、`dcache` 三者的差异串起来。

**步骤**：

1. 准备汇编（满足 spec 的 pipelined memory access 三项前提：全读、同一基址、中间不夹指令）：

   ```asm
   ; 示意汇编：偏移量编码以 u2-l3 / spec 为准
   LW  R1, (R2)
   LW  R3, +4(R2)
   LW  R4, +8(R2)
   LW  R5, +12(R2)
   ```

2. 分别用三组配置编译并运行（或在波形上推演），记录 4 次 `LW` 各自的总线活动与总拍数：
   - 配置 C（`memops`）：`OPT_PIPELINED=0, OPT_LGDCACHE=0`
   - 配置 B（`pipemem`）：`OPT_PIPELINED=1, OPT_LGDCACHE=0`
   - 配置 A（`dcache`，默认）：`OPT_PIPELINED=1, OPT_LGDCACHE=10`

3. 回答三个问题（这是本练习的核心）：

   - **`pipemem` 相比 `memops` 省下的时钟来自何处？**
     来自两点：①`cyc` 在 4 笔访问间常高，省掉了每笔之间拉低/拉高 `cyc` 的空拍；②请求与应答重叠，第 2/3/4 笔的 `stb` 在第 1 笔的 `ack` 尚未返回时就已经发出（FIFO 容纳多个在途请求），从而把首笔的往返延迟「摊」到了整批上，使第 2 笔起接近每条 1 拍。

   - **`dcache` 在其中扮演什么角色？**
     如果这 4 个地址落在同一条缓存行内，`dcache` 会在首次 miss 时一次性把整行突发读进缓存，随后 3 笔全部命中缓存——`o_wb_cyc_gbl` 全程为 0，零总线访问，每笔 1–2 拍返回。换言之，`dcache` 用「整行预取」把空间局部性转化成了延迟收益，连 `pipemem` 那点首笔往返延迟都替后续访问省掉了。

   - **什么情况下 `dcache` 反而不如 `pipemem`？**
     当访问完全不具空间局部性（每次都跨行、每次都 miss）时，`dcache` 每次都要做整行突发填充，搬进来的数据用不上，反而比 `pipemem` 的单笔访问更费总线带宽——这就是 spec 在「Memory Architecture」节强调「必须由地址译码器告诉缓存哪些地址可缓存」的原因：不可缓存地址（如外设、低局部性的存储区）应被排除在缓存之外，走 `DC_READS` 单笔直通路径。

**预期结果**：连续局部性访问下，`dcache`（命中主导）< `pipemem`（流水线）< `memops`（串行）的总拍数。各配置的精确拍数「待本地验证」。

## 6. 本讲小结

- ZipCPU 的数据访存有三种实现——`memops`/`pipemem`/`dcache`，它们 CPU 侧与 Wishbone 侧端口形状几乎一致，被外壳 `zipwb.v` 用一个 `generate if` 三选一，对 `zipcore` 内核完全透明。
- 选型由两个综合期参数决定：`OPT_DCACHE = (OPT_LGDCACHE > 2)` 优先选 `dcache`；否则 `OPT_MEMPIPE = OPT_PIPELINED` 选 `pipemem`；都关则退回 `memops`。默认 `OPT_LGDCACHE=10` → 用 `dcache`。
- `memops` 是单笔交易状态机，最多 1 个在途请求，靠 `o_busy` 反压让上游串行排队，面积最小、最慢。
- `pipemem` 用一个小 FIFO 容纳多个在途请求，突发期间 `cyc` 常高、每拍发一笔 `stb`、每拍收一个 `ack`，把连续访存压到约 1 拍/条；省下的时钟来自「不打散 `cyc`」和「请求/应答重叠」。
- `dcache` 在 `pipemem` 能力之上加数据缓存：命中（最近行单周期 / 其它行两周期）零总线访问，miss 时整行突发填充；写采用写直达策略保持一致；可缓存性由外部 `iscachable` 模块决定。
- spec 把「pipelined memory access」的前提固化为三条：突发内全读或全写、同一基址、中间无停顿或其它指令——这是 `pipemem`/`dcache` 流水线加速能成立的边界条件。

## 7. 下一步学习建议

- **本单元下一讲 u3-l7（流水线冒险与停顿）**：本讲的 `o_busy`/`o_pipe_stalled`/`o_rdbusy` 正是数据冒险与加载延迟（load-use hazard）的关键来源之一。下一讲会把它们纳入整个流水线停顿框架。
- **u4-l1（Wishbone 封装 zipwb 与 zipbones）**：本讲只看了 `zipwb.v` 里「选哪个访存模块」这一段；u4-l1 会把取指与访存如何在 `zipwb` 里仲裁合并成单一 Wishbone 出口讲完整。
- **u4-l3（AXI 与 AXI-Lite 封装）**：AXI 侧有与 `memops`/`pipemem`/`dcache` 一一对应的 `axilops`/`axilpipe`/`axipipe`/`axidcache`，可对照本讲理解「换总线协议但访存策略不变」的模块族设计。
- **u5-l2（形式化验证体系）**：三个模块末尾都有大段 `fwb_master`/`fmem` 形式化属性（本讲多次引用其断言），u5-l2 会解释这些断言为何只含属性、不含功能逻辑，以及如何用 SymbiYosys 跑通它们。
