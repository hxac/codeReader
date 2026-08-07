# 总线支持模块 rtl/ex

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `wbpriarbiter` 两个主设备同时请求总线时谁优先、为什么，并理解它的「双线」变体 `wbdblpriarb` 解决了什么额外的时序问题；
- 解释 `busdelay` 为什么要把一次 Wishbone 访问延迟一拍，以及它在什么场合才需要；
- 区分 `sfifo` 与 `skidbuffer` 这两个缓冲模块各自解决的是「同一个问题还是不同问题」；
- 明白 `fwb_master` / `fwb_slave` 是**形式化属性封装**，里面没有功能逻辑，只是用断言把 Wishbone 的总线契约写下来交给 SymbiYosys 去证明。

本讲是第 4 单元「总线封装、系统整合与外设」的一环。在上一讲（u4-l1）你已经看到 `zipwb` 用 `wbdblpriarb` 把取指与访存两条主设备合并成一条对外 Wishbone 出口；本讲就走进 `rtl/ex/` 目录，把这一层「夹在主设备和总线之间的辅助小模块」逐个拆开看清楚。

## 2. 前置知识

在进入源码之前，先用一句话复习几个反复出现的概念（细节可回看 u3-l6 与 u4-l1）：

- **Wishbone 主/从端口**：主设备（master）发起 `cyc/stb/we/adr/dat/sel`，从设备（slave）回 `ack/stall/err`。一次访问以 `cyc` 拉高开始、拉低结束。
- **背压（backpressure / stall）**：当下游来不及接收时，用 `stall`（或 `!ready`）顶住上游，让上游这一拍「别动」。背压是流水线总线的命脉。
- **在途请求（outstanding requests）**：已经发出、但还没收到 `ack` 的请求数。`pipemem`、`axilpipe` 之所以快，就是因为允许同时在途多个请求。
- **综合期参数（OPT_*）**：不是运行时开关，而是综合时的「剪刀」，决定要不要生成某块电路。本讲的 `busdelay`、`skidbuffer` 也都靠这类参数裁剪行为。
- **形式化验证（formal）**：用数学方法证明电路在所有合法输入下都满足某些性质，而不是跑有限个测试用例。ZipCPU 用 SymbiYosys（`.sby`）来做这件事，详见 u5-l2。

`rtl/ex/` 这个目录名里的 `ex`，可以理解为「extras / 辅助」。它装的不是 CPU 内核，也不是外设，而是把内核和外设「接到总线上去」所必需的一圈胶水：仲裁器、延迟器、缓冲器、以及给形式化验证用的属性封装。本讲涉及的关键文件如下表。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| `rtl/ex/wbpriarbiter.v` | 两个 Wishbone 主设备合到一条总线的**优先级仲裁器**（端口 A 常驻优先） | 是 |
| `rtl/ex/wbdblpriarb.v` | 「双线」优先级仲裁器，每个主设备带本地/全局两组 `cyc/stb`，把地址判决提前一拍 | 简要对比 |
| `rtl/ex/busdelay.v` | 把一次总线访问整体延迟一拍，用于缓解时序收敛 | 是（含使用点） |
| `rtl/ex/sfifo.v` | 通用同步 FIFO，深度 `2^LGFLEN`，用于平滑生产/消费速率 | 是 |
| `rtl/ex/skidbuffer.v` | 深度为 1 的 SKID 缓冲，用于 AXI 注册输出的停顿传播问题 | 是 |
| `rtl/ex/fwb_master.v` | Wishbone **主设备侧**的形式化属性封装（只有断言，无功能逻辑） | 是 |
| `rtl/ex/fwb_slave.v` | Wishbone **从设备侧**的形式化属性封装（同上） | 对比提及 |

真实使用点（帮你把模块和系统串起来，后面会引用）：

- `wbpriarbiter` 在 `zipsystem.v` 里实例化为 `dmacvcpu`，仲裁「CPU 经 MMU 的访问」与「DMA（flash cache）的访问」。
- `wbdblpriarb` 在 `zipwb.v` 里被实例化两次，分别处理取指与访存的「本地总线 / 全局总线」分流与仲裁（u4-l1 已讲）。
- `skidbuffer` 在 AXI 系外壳（`zipaxil.v`、`zipaxi.v`）里大量用于各通道的注册输出。
- `sfifo` 在 `axilfetch.v`、`pffifo.v`、`zipdma.v` 里用作预取/数据缓冲。

## 4. 核心概念与源码讲解

### 4.1 优先级仲裁器 wbpriarbiter（及双线变体 wbdblpriarb）

#### 4.1.1 概念说明

当一个系统里有两个主设备要共享同一条 Wishbone 总线时，就需要一个**仲裁器（arbiter）**来决定「这一拍总线归谁」。ZipCPU 在 `rtl/ex/` 里提供了两个版本：

- `wbpriarbiter`：标准版，两个主设备 A、B，端口 A 拥有**常驻优先权**。
- `wbdblpriarb`：双线版，每个主设备额外提供「本地总线 / 全局总线」两组 `cyc/stb`，目的是把「这次访问落在哪段地址」的判决**提前一个时钟**做出来，缓解片内外设的时序压力。

为什么要分这两个版本？`wbpriarbiter` 的文件头讲得很直白：它的目标是「消除另一个仲裁器里需要的组合逻辑，同时仍保证优先通道的访问时间」。而 `wbdblpriarb` 的文件头则说明：如果只用单组 `cyc/stb`，那么外设在一个时钟内要做两次比较——①这访问是本地总线还是外部总线，②这访问是不是发给我的——结果 ZipCPU 在板子上无法满足时序。把 `cyc/stb` 拆成本地/全局两组，就能把第①步判决挪到前一个时钟去做，给第②步留出时间。

#### 4.1.2 核心流程

`wbpriarbiter` 的全部仲裁逻辑只靠**一个寄存器** `r_a_owner`（「现在是不是 A 在占用总线」），规则有四条：

1. 总线空闲时，A 永远是默认主人，访问直通、零延迟。
2. 当 B 拉起 `cyc`（且 `stb`）而 A 没有占用时，B 抢到总线。
3. 谁抢到，只要它的 `cyc` 还高着，就一直是它。
4. `cyc` 一拉低，所有权立刻归还 A。

换句话说，B 是「蹭车」的：只有 A 不用车的时候 B 才能上，A 一要车 B 就得让。这是一个**严格优先、非公平**的仲裁——它换来的是 A 路径上几乎没有组合逻辑延迟。

输出侧是一个简单的多路选择：`o_cyc/o_we/o_stb/o_adr/o_dat/o_sel` 全部由 `r_a_owner` 在 A、B 两路之间二选一；从设备回来的 `ack/stall/err` 则**只路由给当前的主人**，非主人那一路的 `ack/err` 被强制为 0、`stall` 被强制为 1（让它停着别动）。

#### 4.1.3 源码精读

仲裁的唯一状态位 `r_a_owner`，复位默认为 1（A 占有）：

[wbpriarbiter.v:108-115](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbpriarbiter.v#L108-L115) —— `r_a_owner`：B 不请求则 A 占有；B 请求且 `stb` 且 A 不占有则 B 抢占。

注意第 113 行的判定带了 `(i_b_stb)`：B 单独拉 `cyc` 但不发 `stb` 时并不会抢占总线，这让 B 可以提前「占线做准备」而不真正夺走总线。

输出 `cyc/stb/we` 的多路选择：

[wbpriarbiter.v:125-127](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbpriarbiter.v#L125-L127) —— 由 `r_a_owner` 在 A、B 之间选一路输出到合并总线。

回程信号（默认 `OPT_LOWLOGIC` 分支）只发给当前主人：

[wbpriarbiter.v:154-165](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbpriarbiter.v#L154-L165) —— `ack/err` 只在「你是主人」时透传，否则为 0；`stall` 只在「你是主人」时跟随下游，否则恒为 1（强制停顿）。

这一段最关键的是第 159–160 行的两个 `stall` 赋值：**没有拿到总线的那一路，`o_*_stall` 被钉死在 1**。这正是「背压」的体现——你抢不到总线，就得停着等。

真实使用点：在 `zipsystem.v` 里，CPU 经 MMU 的访存路径与 flash cache（DMA）共享外部总线，用 `wbpriarbiter` 实例 `dmacvcpu` 仲裁，A 路接 MMU（CPU），B 路接 flash cache：

[zipsystem.v:1825-1840](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1825-L1840) —— 实例 `dmacvcpu`：CPU（经 MMU）走 A 路、享有优先，DMA 蹭 B 路；注释里写明「CPU 会在 flash cache 拿到总线后停住，直到它用完」。

至于「双线」变体 `wbdblpriarb`，它的端口与 `wbpriarbiter` 几乎一样，唯一区别是每个主设备的 `cyc/stb` 各拆成两组（本地、全局），仲裁规则依旧是基于 `r_a_owner` 的严格优先：

[wbdblpriarb.v:98-99](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbdblpriarb.v#L98-L99) —— A 路带 `i_a_cyc_a / i_a_cyc_b / i_a_stb_a / i_a_stb_b`，分别对应本地段与外部段。

这样外设就不必在同一个时钟里既判「本地还是外部」又判「是不是我」，时序因此宽松下来。这也是上一讲 `zipwb` 选用 `wbdblpriarb` 而非 `wbpriarbiter` 的原因。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「两个主设备同时请求时谁赢」。
2. **操作步骤**：
   - 打开 `rtl/ex/wbpriarbiter.v`，定位第 109–114 行的 `r_a_owner` always 块。
   - 假设当前 `r_a_owner == 1`（A 占有），且某一拍 A、B **同时**拉起 `i_a_cyc=1, i_a_stb=1` 与 `i_b_cyc=1, i_b_stb=1`。
   - 套用规则推演：因为 `i_b_cyc` 为 1，进入 `else if`；但 `!i_a_cyc` 为假（A 也在请求），所以 `r_a_owner` 保持 1。
3. **需要观察的现象**：下一个时钟 `r_a_owner` 仍为 1，于是 `o_cyc = i_a_cyc`、`o_a_stall` 跟随下游、而 `o_b_stall` 被钉成 1。
4. **预期结果**：A 优先、B 被停顿。结论——**只要 A 的 `cyc` 还在，B 永远抢不到**，即便 B 比 A 更早「想要」总线。这就是「严格优先、非公平」的含义。
5. 进一步思考：如果想让 DMA 长时间占用总线而不让 CPU 永远等着，这套电路是否够用？答案是不够，因为它没有「公平轮转」机制——这正是它的设计取舍（省组合逻辑换简单）。

#### 4.1.5 小练习与答案

**练习 1**：`wbpriarbiter` 里 `r_a_owner` 的复位值为什么是 1 而不是 0？

**参考答案**：因为 A 是优先通道，复位后总线应默认属于 A，这样 A 的第一次访问可以零延迟直通，无需等仲裁器翻转。

**练习 2**：`wbdblpriarb` 相比 `wbpriarbiter` 多出哪几根信号？解决了什么问题？

**参考答案**：每个主设备多出 `cyc_b/stb_b`（全局段）一对，与原有的 `cyc_a/stb_a`（本地段）并列。它把「访问落在本地段还是外部段」的判决提前一拍，使外设在当前拍只需判断「是不是发给我的」，从而缓解时序收敛。

---

### 4.2 总线延迟 busdelay

#### 4.2.1 概念说明

`busdelay` 的作用用一句话讲：**把一次 Wishbone 访问整体往后推一个时钟**。它不是功能需要，而是**时序需要**。

文件头讲了一段很实在的背景：最早的 ZipSystem 放到某块板子上时无法满足时序，于是加了这层延迟来救场。难点集中在 `stall` 这根线上——Wishbone 规定主设备必须在**第一个时钟**就知道总线会不会 stall。可如果从设备端的 stall 是经过一串组合逻辑才算出来的，主设备根本来不及在当拍反应。

于是作者写了两个版本：原版只延迟 `stb` 等控制信号（`DELAY_STALL=0`），新版连 `stall` 线也一起延迟（`DELAY_STALL=1`，内部用 SKID 思路实现）。文件头明确提醒：**用不上就别开** `DELAY_STALL`，它会消耗资源、进一步拖慢总线；但真到了时序过不去的时候，也别不敢用。

#### 4.2.2 核心流程

`busdelay` 对外仍是标准的 Wishbone 主/从两套端口，中间插入一拍寄存器：

- 输入侧（master bus）：吃进来的 `i_wb_cyc/stb/we/addr/data/sel`，输出回 `o_wb_stall/ack/data/err`。
- 输出侧（delayed bus）：把上述请求寄存一拍后变成 `o_dly_cyc/stb/we/addr/data/sel`，并接收下游的 `i_dly_stall/ack/data/err`。

两个版本的差异在于 `stall` 怎么处理：

- `DELAY_STALL=0`（原版）：请求延迟一拍，但 `stall` 直通。
- `DELAY_STALL=1`（新版）：连 `stall` 也延迟，内部用一个 SKID 缓冲接住「stall 还没传到上游」时那一拍的数据。

无论是哪个版本，文件头都保证：**流水线模式下每拍仍只处理一次访问**，只是 `stb` 拉高与真正完成之间多了一个时钟。

#### 4.2.3 源码精读

模块端口清晰呈现了「主侧 ↔ 延迟侧」的镜像结构：

[busdelay.v:65-100](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/busdelay.v#L65-L100) —— `busdelay` 的参数与端口：左侧 `i_wb_*`/`o_wb_*` 是主侧，右侧 `o_dly_*`/`i_dly_*` 是延迟一拍后的侧；参数 `DELAY_STALL` 与 `OPT_LOWPOWER` 控制两种实现。

文件头对「为什么需要它」的原始说明，是最权威的背景资料：

[busdelay.v:7-17](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/busdelay.v#L7-L17) —— 作者自述：时序不达标时用它救场，难点在于 stall 必须在第一个时钟就让主设备知道。

实现分流由一个 `generate if (DELAY_STALL)` 切开，新版那一支直接起名叫 `SKIDBUFFER`，提示它内部就是用 SKID 缓冲来吸收一拍的：

[busdelay.v:106-130](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/busdelay.v#L106-L130) —— `DELAY_STALL=1` 分支用一组寄存器 `r_stb/r_we/r_addr/r_data/r_sel` 配合下游 stall，实现「请求寄存 + 数据暂存」。

真实使用点：`zipsystem.v` 在仲裁完外部总线后，用 `busdelay` 把访问推迟一拍，由综合期开关 `DELAY_EXT_BUS` 控制是否启用，且这里刻意选了 `DELAY_STALL(0)`（只延迟请求、不动 stall）：

[zipsystem.v:1862-1879](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1862-L1879) —— `generate if (DELAY_EXT_BUS)` 包裹的 `extbus`，把 `ext_*` 内部总线延迟一拍再送到对外端口 `o_wb_*`。

#### 4.2.4 代码实践

1. **实践目标**：理解 `busdelay` 是「可选的时序救火队」，而非默认必装。
2. **操作步骤**：
   - 在 `zipsystem.v` 中搜索 `DELAY_EXT_BUS`，看它作为顶层参数的默认值与声明位置。
   - 对照第 1862 行的 `generate if (DELAY_EXT_BUS)`：只有该参数为真时才会综合出 `extbus` 这个 `busdelay` 实例；为假时外部总线直通、零额外延迟。
3. **需要观察的现象**：`busdelay` 被 `generate if` 包着，说明它是一个**可裁掉的、纯时序用途**的模块。
4. **预期结果**：你能解释「为什么默认可能不开 `DELAY_EXT_BUS`」——因为它只为时序收敛服务，开启会多花一拍延迟与资源。**待本地验证**：若你能综合工程，可分别设 `DELAY_EXT_BUS=0/1` 比较对外 `o_wb_stb` 相对 CPU 请求的相位差。
5. 源码阅读型结论：`busdelay` 是 `OPT_*` 设计哲学的一个典型例子——同一份 RTL，靠综合期参数决定要不要这块「补丁」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DELAY_STALL` 文件头说「能不开就不开」？

**参考答案**：因为它会额外消耗寄存器资源，并且把 stall 也延迟一拍意味着总线整体更慢；它只在你确实无法满足时序时才该启用。

**练习 2**：`busdelay` 改变访问的「内容」吗？

**参考答案**：不改变。它只是把同一组 `cyc/stb/we/addr/data/sel` 往后推迟一个时钟，访问的地址、数据、读写方向完全不变，属于纯时序整形。

---

### 4.3 缓冲双雄：sfifo 与 skidbuffer

这两个模块都和「背压」有关，但它们解决的是**不同层次**的问题。本节先分别讲清，再在实践里做对比。

#### 4.3.1 概念说明

**sfifo —— 通用同步 FIFO。** 它是一个深度可配（`2^LGFLEN`）的循环队列，用来把「生产者」和「消费者」的速率解耦。比如取指模块预取了一串指令进 FIFO，CPU 一条条取出来执行；当 CPU 停在分支上不取时，FIFO 帮你把预取的指令暂存住，不丢、也不阻塞总线。它的关键词是**弹性（elasticity）**：能容纳多个元素，平滑突发。

**skidbuffer —— 深度为 1 的 SKID 缓冲。** 它存在的原因非常具体，文件头讲得透彻：AXI 规范要求**所有输出都必须寄存**。于是当下游计算出 stall 时，这个 stall 要等一个时钟才能传到上游。在 stall「还在路上」的这一拍里，上游送过来的那一个数据必须有人接住，否则就丢了。skidbuffer 就是那个「接住一个」的缓冲。它的关键词是**协议合规（protocol compliance）**：恰好容纳一个元素，专为注册输出的停顿延迟而生。

一句话总结二者关系：**它们都和背压打交道，但 sfifo 解决的是「容量/速率」问题（要装很多个），skidbuffer 解决的是「一拍停顿延迟」问题（只需装一个）**。所以答案是「相关但不同」。更详细对比见 4.3.4。

#### 4.3.2 核心流程

**sfifo** 的读写各走一套指针：

- 维护写指针 `wr_addr`、读指针 `rd_addr`（均为 `LGFLEN+1` 位，靠多出的一位区分「满」与「空」）。
- 填充量 `o_fill = wr_addr - rd_addr`；容量 `FLEN = 2^LGFLEN`，`o_fill` 取值范围 `0 .. 2^LGFLEN`。
- 满了就 `o_full=1`（拒绝再写）、空了就 `o_empty=1`（拒绝再读）；同时读写时 `o_fill` 不变。
- 读端口有两种风格：异步读（`OPT_ASYNC_READ=1`，当拍出数据）或寄存读（当拍读、下拍出数据）。

**skidbuffer** 是一组 `valid/ready` 握手（注意它用的是 AXI 风格的 `valid/ready`，不是 Wishbone 的 `stall`）：

- 入口：`i_valid`/`i_data`/`o_ready`；出口：`o_valid`/`o_data`/`i_ready`。
- 内部只有一个槽 `r_data`（带 `r_valid` 标志）。`o_ready = !r_valid`：只要槽是空的就随时能收。
- 当「上游有数据（`i_valid && o_ready`）」且「下游正停顿（`o_valid && !i_ready`）」同时成立时，把上游这拍数据扣进 `r_data`、置 `r_valid=1`，并在下一拍把它顶上去。
- 出口可选寄存（`OPT_OUTREG`），还有个仅供形式化用的 `OPT_PASSTHROUGH`（直通、不做缓冲）。

#### 4.3.3 源码精读

**sfifo** 的参数与端口——注意深度由 `LGFLEN` 决定，数据宽度由 `BW` 决定：

[sfifo.v:26-48](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/sfifo.v#L26-L48) —— `sfifo` 参数：`BW` 数据宽、`LGFLEN` 深度对数、`OPT_ASYNC_READ` 异步读、`OPT_WRITE_ON_FULL`/`OPT_READ_ON_EMPTY` 边界行为；端口分写半边（`i_wr/i_data/o_full/o_fill`）与读半边（`i_rd/o_data/o_empty`）。

填充量的核心计算——同时读写时保持不变，只写则加一、只读则减一：

[sfifo.v:84-92](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/sfifo.v#L84-L92) —— `o_fill` 用 `case({w_wr, w_rd})` 更新；`default` 分支用 `wr_addr - rd_addr` 自校正。

满标志的产生——容量是 `2^LGFLEN`，写到位时置满：

[sfifo.v:97-108](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/sfifo.v#L97-L108) —— `r_full` 在「只写且当前已 `2^LGFLEN-1`」时下一拍置 1，在「只读」时清 0；`o_full` 再叠加 `OPT_WRITE_ON_FULL` 选项。

存储体与异步读——`FLEN` 个元素的循环缓冲，读地址当拍直出：

[sfifo.v:122-124](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/sfifo.v#L122-L124) 与 [sfifo.v:173-179](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/sfifo.v#L173-L179) —— 写入 `mem[wr_addr]`；异步读分支里 `o_data = mem[rd_addr]`。

**skidbuffer** 的文件头对「为什么需要它」的说明是本节最该精读的文字：

[skidbuffer.v:7-31](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/skidbuffer.v#L7-L31) —— 解释 AXI 输出必须寄存 → stall 要一拍才能上传 → 这一拍的数据必须有人接住 → skidbuffer 就是干这个的。

唯一缓冲槽 `r_valid` 的置位条件——「上游在送、且下游在停」时把数据扣下：

[skidbuffer.v:138-147](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/skidbuffer.v#L138-L147) —— `(i_valid && o_ready) && (o_valid && !i_ready)` 时 `r_valid<=1`；下游 `i_ready` 一来就清 0。

入口随时可收（只要槽空）：

[skidbuffer.v:163-166](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/skidbuffer.v#L163-L166) —— `o_ready = !r_valid`，深度为 1 的直接体现。

真实使用点：`zipaxil.v` 在调试从端口的写地址（AW）通道上挂了一个 `skidbuffer`，把 AXI 的 AWVALID/AWREADY 握手缓冲起来：

[zipaxil.v:402-417](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L402-L417) —— 实例 `dbgawskd`：入口接 `S_DBG_AWVALID/S_DBG_AWREADY`，出口给内部写逻辑，正是 AXI 注册输出场景。

#### 4.3.4 代码实践

1. **实践目标**：回答本讲开篇的问题——`sfifo` 与 `skidbuffer` 解决的是同一个问题（背压）还是不同问题？
2. **操作步骤**：
   - 打开 `sfifo.v`，确认它有 `mem[0:FLEN-1]` 一片存储体、两个指针、`o_fill` 计数（第 68–92 行）。深度由 `LGFLEN` 决定，可装很多个。
   - 打开 `skidbuffer.v`，确认它只有**一个** `r_data`/`r_valid` 槽（第 133–134 行），`o_ready = !r_valid`。
   - 阅读二者文件头关于「Purpose」的描述：`sfifo` 是「synchronous data FIFO」，`skidbuffer` 明确写「required for high throughput AXI code, since the AXI spec requires that all outputs be registered」。
3. **需要观察的现象**：`skidbuffer` 的存在理由被绑死在「注册输出导致 stall 滞后一拍」这一协议约束上；`sfifo` 没有这个约束，纯粹是为了平滑速率。
4. **预期结果**（答案）：
   - 二者都「与背压相关」，但**不是同一个问题**。
   - `skidbuffer` 解决的是 **AXI 注册输出的 1 拍停顿传播问题**，容量恒为 1，是协议合规的「最小补丁」，几乎是 AXI 通道的标配。
   - `sfifo` 解决的是 **生产/消费速率不匹配的弹性缓冲问题**，容量 `2^LGFLEN` 可大可小，用于预取、突发、跨时钟域等需要「囤货」的场合。
   - 可以粗略地把 `skidbuffer` 看成「深度为 1、带 AXI 握手、专门接住滞后的那一个数据」的特殊 FIFO，但它的设计动机和典型用法与通用 FIFO 截然不同。
5. 加分项：在 `sim/rtl/axixbar.v` 里搜索 `skidbuffer`，你会看到 AXI 交换器在每个通道（AW/W/B/AR/R）都各挂一个 skidbuffer——这正是因为 AXI 五通道全都要求注册输出。

#### 4.3.5 小练习与答案

**练习 1**：`sfifo` 的 `o_fill` 用了 `LGFLEN+1` 位指针，为什么要比地址多一位？

**参考答案**：用多出的一位来区分「满」与「空」两种指针相等的情况；否则光靠 `wr_addr == rd_addr` 无法分辨队列是满的还是空的。

**练习 2**：把 `skidbuffer` 的 `OPT_PASSTHROUGH` 设为 1 会怎样？它还能接住滞后的数据吗？

**参考答案**：不能。`OPT_PASSTHROUGH=1` 时模块退化成直通（`o_valid=i_valid; o_ready=i_ready`），完全不做缓冲，仅供形式化验证时充当「无缓冲」对照，不用于真实 AXI 通道。

**练习 3**：一个 AXI 主设备的写数据（W）通道如果**不加** skidbuffer，可能会出什么问题？

**参考答案**：因为输出寄存，下游的 `WREADY=0`（停顿）要晚一拍才能传回主设备；这一拍里主设备仍在送 `WDATA`，没人接住就会丢失一个数据拍，违反 AXI 协议。

---

### 4.4 形式化属性封装 fwb_master / fwb_slave

#### 4.4.1 概念说明

`fwb_master` 和 `fwb_slave` 是 `rtl/ex/` 里最容易被误读的两个文件：它们看起来像普通的 Wishbone 接口模块，**但实际上不含任何功能逻辑**，只是把 Wishbone 总线的「契约」用断言（assert / assume）写下来，交给 SymbiYosys 去做形式化证明。

文件头强调得很明确：本模块**没有功能逻辑，仅供形式化验证**；它输出的 `f_nreqs / f_nacks / f_outstanding`（请求数、应答数、在途请求数）也只是给后续的形式化证明当「计数器工具」用，不是给真实电路用的。

两者的区别在于**视角**：

- `fwb_master`：站在**主设备**角度。对主设备的**输出**（`cyc/stb/we/addr/data/sel`）做**断言**（assert，证明它合法），对主设备的**输入**（`stall/ack/data/err`，即从设备的回应）做**假设**（assume，假设从设备守规矩）。
- `fwb_slave`：正好相反，站在**从设备**角度。对从设备的**输入**（即主设备输出）做假设，对从设备的**输出**做断言。

为了让两个文件尽量像「同一份契约的两面」、便于对照，作者用了一对宏 `SLAVE_ASSUME` / `SLAVE_ASSERT`：在 `fwb_master` 里把它们定义成 `assert` / `assume`，在 `fwb_slave` 里对调过来。这样 diff 两个文件时，看到的差异才是真正的「视角差异」，而不是宏名翻转造成的噪声。

#### 4.4.2 核心流程

`fwb_master` 大致做了这几类性质检查（每一类对应文件里一段）：

1. **复位/初值**：复位期间 `cyc/stb` 必须为 0，从设备不应在没请求时回 `ack/err`。
2. **请求合法性**：`stb` 为真时 `cyc` 必须为真；被 stall 住的请求，其 `we/addr/sel/data` 必须保持稳定（不能在被顶住时乱改）；一次 bus cycle 里读写方向不能乱跳。
3. **应答合法性**：`cyc` 已落下且没有在途请求时，不应再回 `ack/err`；`ack` 与 `err` 不能同拍为真。
4. **停顿与延迟上界**（可选）：`F_MAX_STALL` 限制从设备最多连续 stall 几拍；`F_MAX_ACK_DELAY` 限制请求后多久内必须给应答。
5. **在途请求计数**：`f_nreqs` 统计 `stb && !stall` 的次数，`f_nacks` 统计 `ack || err` 的次数，`f_outstanding = nreqs - nacks`，并断言它不超过 `F_MAX_REQUESTS`、不超过计数器上限。

关键计数三条等式：

- 请求计数：每当 `i_wb_stb && !i_wb_stall` 加一（`cyc` 掉则清零）。
- 应答计数：每当 `i_wb_ack || i_wb_err` 加一。
- 在途：\( f_{outstanding} = f_{nreqs} - f_{nacks} \)（仅在 `cyc` 有效时，否则为 0）。

#### 4.4.3 源码精读

文件头对「无功能逻辑、仅形式化」的郑重声明：

[fwb_master.v:7-24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L7-L24) —— 明说本模块无功能逻辑，输出计数仅供形式化用；并解释与 `fwb_slave` 的视角差异。

视角翻转的两个宏（这是理解 `fwb_master` 与 `fwb_slave` 互为镜像的钥匙）：

[fwb_master.v:136-137](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L136-L137) —— 在 master 里 `SLAVE_ASSUME` 被定义成 `assert`、`SLAVE_ASSERT` 被定义成 `assume`；slave 文件里两者对调。

三个计数器的实现——这是「在途请求」概念在代码里的落点：

[fwb_master.v:398-416](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L398-L416) —— `f_nreqs` 在 `stb && !stall` 时加一，`f_nacks` 在 `ack || err` 时加一，二者均在 `cyc` 失效时清零。

在途请求数的组合计算与上界断言：

[fwb_master.v:422-435](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/fwb_master.v#L422-L435) —— `f_outstanding = (i_wb_cyc) ? (f_nreqs - f_nacks) : 0`，并断言 `f_outstanding < MAX_OUTSTANDING`、`f_nacks <= f_nreqs`。

它怎么被别的模块「用」上？回到本讲的 `wbpriarbiter`：在它的 `FORMAL` 段里，分别用 `fwb_master` 套住合并后的输出总线、用 `fwb_slave` 套住 A 路和 B 路两个输入，从而在形式化证明中同时约束「对外是合法主设备」「对 A/B 是合法从设备」：

[wbpriarbiter.v:225-274](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbpriarbiter.v#L225-L274) —— `f_wbm`（fwb_master）约束对外总线，`f_wba`/`f_wbb`（fwb_slave）约束 A、B 两路输入；这正是「属性封装」的典型用法。

`fwb_slave` 的文件头同样声明自己无功能逻辑，只是从从设备视角写同一份契约：

`rtl/ex/fwb_slave.v` 文件头第 3–13 行——assumptions 打在 slave 的输入（即主设备输出），assertions 打在 slave 的输出（即对主设备的回应）。它和 `fwb_master` 是同一套 Wishbone 规则的镜像两面。

#### 4.4.4 代码实践

1. **实践目标**：亲手确认 `fwb_master` 里**没有一条会改变电路行为的赋值**，全是 `assert/assume`。
2. **操作步骤**：
   - 打开 `rtl/ex/fwb_master.v`，从模块声明（第 66 行）往后通览。
   - 注意所有 `always @(posedge i_clk)` 块里，寄存器只有 `f_past_valid`、`f_nreqs`、`f_nacks` 以及各类 `f_*_count`——它们的更新**只服务于断言**，不驱动任何对外端口。
   - 对外端口 `f_nreqs/f_nacks/f_outstanding` 是给外层证明当「探针」用的，真实综合时会因为整个模块被 `\`ifdef FORMAL` 保护而不存在。
3. **需要观察的现象**：你会发现模块里没有任何 `assign o_xxx = ...` 去驱动真正的总线信号；它只是「旁听」`i_wb_*` 信号并检查它们。
4. **预期结果**：能向别人解释「为什么 `fwb_master` 可以原样接到任何 Wishbone 主设备上而不改变其行为」——因为它只读不写、只在形式化时存在。**待本地验证**：在本讲第 5 节的形式化练习里，你会真正跑一个用到它的证明。
5. 进阶阅读：看 `wbpriarbiter.v` 第 278–289 行，那里用 `f_a_outstanding == f_outstanding` 这类断言把「A 路在途数 == 总线在途数（当 A 占有时）」这条不变量写死，正是借助 `fwb_master/slave` 提供的计数探针实现的。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fwb_master` 对自己的输出（主设备的 `cyc/stb/...`）用 `assert`，而对输入（从设备的 `ack/stall/err`）用 `assume`？

**参考答案**：因为它在证明「我这个主设备是守规矩的」，所以对自己发出的信号要 `assert`（证明成立）；而从设备的回应不是它能控制的，只能 `assume`（假设对方也守规矩），把责任边界划清。

**练习 2**：`f_outstanding` 这个数在真实综合后的电路里存在吗？

**参考答案**：不存在。`fwb_master` 整体在 `\`ifdef FORMAL` 之下，只在形式化验证工具里被编译，综合时会被剔除，因此 `f_outstanding` 不会变成真实硬件。

**练习 3**：`SLAVE_ASSUME` / `SLAVE_ASSERT` 这对宏的设计意图是什么？

**参考答案**：让 `fwb_master` 和 `fwb_slave` 两个文件能共用几乎相同的断言文本，只在「哪边是 assert、哪边是 assume」上翻转；这样对照两份文件时，diff 反映的是真正的视角差异，便于维护和审查。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「源码阅读 + 推理」的综合任务，目标是为一个虚构的小系统选型并解释理由。

**场景**：你要给 ZipCPU 接一个自定义 AXI 从设备外设，并且 CPU 与一个 DMA 要共享外部 Wishbone 主存。

**任务**：

1. **仲裁选型**：CPU 和 DMA 共享外部 Wishbone 总线，应选 `wbpriarbiter` 还是 `wbdblpriarb`？请根据 4.1 的说明给出判断依据（提示：是否需要提前一拍区分本地/外部段）。引用 `zipsystem.v:1825` 的实例说明现实工程里选了哪个、为什么 CPU 放在 A 路。
2. **时序救火**：若外部总线时序不达标，你会启用哪个模块、哪个参数？写出它在 `zipsystem.v` 里的实例名与控制它的综合期参数名（提示：`busdelay` / `DELAY_EXT_BUS`）。
3. **AXI 缓冲**：为自定义 AXI 从设备的写地址通道选一个缓冲模块，说明为什么用 `skidbuffer` 而不是 `sfifo`，并指出它解决的是「容量」还是「1 拍停顿延迟」问题。引用 `zipaxil.v:402` 作为现实范例。
4. **契约证明**：如果你要形式化证明「这个 AXI 从设备正确响应 Wishbone/Wishbone 风格请求」，应该用 `fwb_master` 还是 `fwb_slave` 套在它哪一侧？说明 assert/assume 的方向。

**预期产出**：一份表格，列出每个决策点选择的模块、引用的源码行号、以及一句话理由。完成后，你就把本讲的「仲裁—延迟—缓冲—形式化契约」四件事串成了一条真实的工程决策链。

## 6. 本讲小结

- `wbpriarbiter` 用一个 `r_a_owner` 寄存器实现严格优先（A 常驻、B 蹭用）的非公平仲裁，非主人那一路被钉死 `stall=1`；`wbdblpriarb` 是其「双线」变体，靠本地/全局两组 `cyc/stb` 把地址判决提前一拍，缓解外设时序。
- `busdelay` 是可选的时序救火队，把一次访问整体推迟一拍，靠 `DELAY_STALL` 决定要不要连 stall 一起延迟；它由综合期参数（如 `DELAY_EXT_BUS`）裁剪，能不开就不开。
- `sfifo` 是深度 `2^LGFLEN` 的通用 FIFO，解决生产/消费**速率弹性**问题；`skidbuffer` 是深度为 1 的 SKID 缓冲，解决 AXI 注册输出导致的** 1 拍停顿传播**问题——二者都与背压相关，但不是同一个问题。
- `fwb_master` / `fwb_slave` 是无功能逻辑的形式化属性封装，分别从主/从视角把 Wishbone 契约写成 assert/assume，靠 `SLAVE_ASSUME/SLAVE_ASSERT` 宏互为镜像，并提供 `f_nreqs/f_nacks/f_outstanding` 给上层证明当探针。
- 这些模块共同体现了 ZipCPU 的 `OPT_*` 综合期裁剪哲学：同一份 RTL，靠参数决定要不要仲裁延迟、要不要缓冲、要不要形式化。

## 7. 下一步学习建议

- 想看 `wbdblpriarb` 在系统里怎么和取指/访存配合？回看上一讲 **u4-l1**（`zipwb` 的双优先仲裁），或继续 **u4-l2**（`zipsystem` 内部总线拓扑，那里 `wbpriarbiter` 与 `busdelay` 同时出场）。
- 想真正跑一次用到 `fwb_master/slave` 的形式化证明？进入 **u5-l2（形式化验证体系）**，它会带你用 `bench/formal` 下的 `.sby` 跑通一个证明。
- 对 AXI 五通道为什么处处是 skidbuffer 想刨根问底？继续 **u4-l3（AXI 与 AXI-Lite 封装）**，以及 `sim/rtl/axixbar.v` 这个 AXI 交换器实例。
- 若要自己搭一个带这些胶水模块的小 SoC，**u5-l7（自定义 SoC 集成）** 会把地址译码、总线互连和本讲的仲裁/缓冲串成一个完整工程。
