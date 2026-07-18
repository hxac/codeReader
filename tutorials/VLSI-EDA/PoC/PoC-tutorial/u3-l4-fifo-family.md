# FIFO 家族

## 1. 本讲目标

FIFO（First-In First-Out，先入先出队列）是数字电路里最常用的「数据缓冲与解耦」组件之一。PoC 在 `src/fifo/` 下提供了一整套 FIFO 矩阵，覆盖同钟、跨钟、小容量、暂存回滚等多种场景。

学完本讲后，你应该能够：

- 区分 PoC 的各类 FIFO（`fifo_glue` / `fifo_shift` / `fifo_cc_got` / `fifo_ic_got` / `fifo_cc_got_tempput` / `fifo_cc_got_tempgot`），并知道每种该用在什么地方。
- 看懂 PoC FIFO 统一使用的「流水线接口」：`put` / `din` / `full` 写入侧，`got` / `dout` / `valid` 读出侧，以及 first-word-fall-through（FWFT，前置有效）语义。
- 掌握 `DATA_REG` / `STATE_REG` / `OUTPUT_REG` / `*STATE_*_BITS` 这几个关键 generic 的作用，特别是 `estate_wr` / `fstate_rd` 这两个「填充指示器」的含义。
- 能动手实例化一个 `fifo_cc_got` 并配置参数。

---

## 2. 前置知识

在进入本讲前，建议你已经理解（参见前置讲义）：

- **命名空间包模式**（u3-l1）：`src/fifo/fifo.pkg.vhdl` 是这个命名空间的「目录页」，集中声明所有 FIFO 的 component；它必须先于具体核被编译。
- **片上 RAM 抽象 ocram**（u3-l3）：大容量 FIFO 的底层存储来自 `mem` 命名空间下的 `ocram_sdp`（简单双端口 RAM）。本讲的 `fifo_cc_got` / `fifo_ic_got` 都会实例化它。
- **utils 包的辅助函数**（u2-l2）：`log2ceil`、`log2ceilnz`、`imax`、`Is_X`、`SIMULATION` 等会反复出现。

几个本讲会用到的通俗概念：

- **时钟域（clock domain）**：由同一个时钟驱动的所有触发器属于一个时钟域。读写用同一个时钟叫「同钟（common clock, cc）」；读写用两个互不相关的时钟叫「独立钟/跨钟（independent clock, ic）」。
- **亚稳态（metastability）**：跨时钟域传递信号时，信号可能在触发器的建立/保持时间窗口内变化，导致输出在一段时间内既不是 0 也不是 1。跨钟 FIFO 必须用专门的同步机制（格雷码指针 + 多级同步）来规避。
- **FWFT（First-Word-Fall-Through，前置有效）**：一种 FIFO 读接口约定。数据在 `valid` 拉高时就已经出现在 `dout` 上，读侧只需在取走数据当拍把 `got` 拉高一拍即可；不需要先发「读请求」再等一拍拿数据。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [src/fifo/fifo.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl) | 命名空间根包，集中声明全家族所有 FIFO 的 component（无 body）。 |
| [src/fifo/fifo_glue.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_glue.vhdl) | 最小 FIFO，仅 2 个字深度，用于解耦「使能域」。 |
| [src/fifo/fifo_shift.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_shift.vhdl) | 基于移位寄存器的小 FIFO，可映射到 Xilinx SRL。 |
| [src/fifo/fifo_cc_got.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl) | 主力同钟 FIFO，由 ocram 撑底，generic 最丰富。 |
| [src/fifo/fifo_ic_got.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_ic_got.vhdl) | 跨钟 FIFO，用格雷码指针 + 双 FF 同步实现 CDC。 |
| [tb/fifo/fifo_cc_got_tb.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl) | `fifo_cc_got` 的测试台，演示了参数实例化与批量验证。 |

> 说明：PoC 没有 `src/fifo/fifo.files` 这种命名空间级清单，每个 FIFO 各有一份 `<entity>.files`（例如 `fifo_cc_got.files`），由 pyIPCMI 消费，用来拉齐它依赖的公共包、`arith_carrychain_inc`、`ocram_sdp` 等编译顺序。

---

## 4. 核心概念与源码讲解

### 4.1 FIFO 分类：一张矩阵看懂全家族

#### 4.1.1 概念说明

PoC 没有把所有需求塞进一个「超级 FIFO」，而是按两个正交维度切出了一组小而精的核：

1. **时钟关系**：同钟（cc）还是跨钟（ic）。
2. **存储方式与功能**：极小容量解耦、移位寄存器、标准 RAM 支撑、带暂存回滚（commit/rollback）。

这样切分的好处是：每个核只解决一类问题，综合出来的面积/时序代价最小。例如你只需要解耦两拍流水线，用 2 个字的 `fifo_glue` 远比开一个 BRAM 的 `fifo_cc_got` 划算。

家族成员一览（component 声明都在 `fifo.pkg.vhdl`）：

| 核 | 时钟 | 存储实现 | 典型用途 |
|----|------|----------|----------|
| `fifo_glue` | cc | 2 个寄存器 | 解耦处理流水线的使能域 |
| `fifo_shift` | cc | 移位寄存器 | 小容量 FIFO，可映射到 LUT/SRL |
| `fifo_cc_got` | cc | ocram（BRAM/分布式） | 通用大容量同钟 FIFO |
| `fifo_ic_got` | ic（跨钟） | ocram（专用 BRAM） | 跨时钟域数据搬运 |
| `fifo_dc_got_sm` | dc（相关钟） | — | 两个时钟频率相关但不同 |
| `fifo_cc_got_tempput` | cc | ocram | 写入侧可暂存/回滚（commit/rollback） |
| `fifo_cc_got_tempgot` | cc | ocram | 读出侧可暂存/回滚 |
| `fifo_ic_assembly` | ic | ocram | 写地址受限的装配式跨钟 FIFO |
| `fifo_ll_glue` | cc | — | Local-Link 协议的小 FIFO |

本讲重点讲前四个最常用的（`glue` / `shift` / `cc_got` / `ic_got`），后几个作为「知道有这类扩展」即可。

#### 4.1.2 核心流程

选型决策可以这样走：

```text
需要 FIFO 吗？
├─ 只为解耦相邻两拍使能 → fifo_glue（2 字深）
├─ 容量小、想塞进 LUT/SRL → fifo_shift
├─ 读写同钟、容量较大 → fifo_cc_got
│     └─ 还需要写入回滚（投机执行） → fifo_cc_got_tempput
│     └─ 还需要读出回滚 → fifo_cc_got_tempgot
└─ 读写跨时钟域 → fifo_ic_got
```

判断「同钟 vs 跨钟」是第一步且最关键：选错了要么浪费资源（同钟需求用了昂贵的同步器），要么功能错误（跨钟需求用了同钟 FIFO，会采样到亚稳态）。

#### 4.1.3 源码精读

整个家族的 component 声明集中在 `fifo.pkg.vhdl`。先看四个主力核的端口对比（注意命名差异）：

`fifo_glue` 的端口（最小，注意它用 `ful`/`vld`/`di`/`do`，没有 `e/fstate`）：

- [src/fifo/fifo_glue.vhdl:L36-L55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_glue.vhdl#L36-L55) — 这里声明了它「只有 `D_BITS` 一个 generic」，端口仅 `put/di/ful` 写入侧与 `vld/do/got` 读出侧。注释写明它的存储只有两个字。

`fifo_cc_got` 的 generic 与端口（最完整的一套配置旋钮）：

- [src/fifo/fifo.pkg.vhdl:L120-L146](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L120-L146) — 声明 `D_BITS`、`MIN_DEPTH`、`DATA_REG`、`STATE_REG`、`OUTPUT_REG`、`ESTATE_WR_BITS`、`FSTATE_RD_BITS` 七个 generic，以及 `put/din/full/estate_wr` 写入侧与 `got/dout/valid/fstate_rd` 读出侧。

`fifo_ic_got` 的端口（读写各有独立时钟与复位）：

- [src/fifo/fifo.pkg.vhdl:L165-L191](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L165-L191) — 注意它的 generic 比 `cc_got` 少了 `STATE_REG`（跨钟版本只有一种状态实现），且端口分成 `clk_wr/rst_wr` 与 `clk_rd/rst_rd` 两组。

带暂存回滚的 `fifo_cc_got_tempput`（写入侧多出 `commit`/`rollback`）：

- [src/fifo/fifo.pkg.vhdl:L193-L222](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L193-L222) — 在 `fifo_cc_got` 基础上多了 `commit` 与 `rollback` 两个输入，允许把「投机写入」的数据确认或撤销。

> 命名规律小结：`cc`=common clock 同钟，`ic`=independent clock 跨钟，`dc`=dependent clock 相关钟；`got` 表示读侧用 `got` 脉冲确认取数（区别于标准 FIFO 的 `rdreq`）；`tempput`/`tempgot` 表示在写/读侧支持暂存与回滚。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `fifo.pkg.vhdl` 把全家族端口差异看清，建立选型直觉。

**操作步骤**：

1. 打开 [src/fifo/fifo.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl)。
2. 分别定位 `fifo_glue`、`fifo_cc_got`、`fifo_ic_got` 三处 component 声明。
3. 对比三者的端口，记录下「谁是单时钟、谁是双时钟」「谁有 `estate_wr/fstate_rd`、谁没有」。

**需要观察的现象**：

- `fifo_glue` 只有 `clk/rst`，没有 `estate_wr/fstate_rd`。
- `fifo_cc_got` 与 `fifo_ic_got` 都有 `estate_wr/fstate_rd`，但前者只有一对 `clk/rst`，后者有 `clk_wr/rst_wr` 和 `clk_rd/rst_rd`。

**预期结果**：你会直观看到「功能越强 → generic/端口越多」的递进关系，这正好对应面积与时序代价的递增。

#### 4.1.5 小练习与答案

**练习 1**：你需要把一个 200 MHz 域的数据流搬到一个 50 MHz 域，应该选哪个 FIFO？为什么不能选 `fifo_cc_got`？

**参考答案**：选 `fifo_ic_got`。两个时钟频率不同且不相关，属于跨时钟域，必须用带格雷码指针同步器的跨钟 FIFO。`fifo_cc_got` 要求读写同钟，跨钟使用会采样到亚稳态，导致数据错乱甚至指针跑飞。

**练习 2**：一条流水线里，上一级偶尔停一拍、下一级也偶尔停一拍，你想让两级之间不互相卡死，深度只需 2，选哪个？

**参考答案**：选 `fifo_glue`。它的设计目的就是「解耦使能域」，且存储恰好只有两个字，`ful`/`vld` 都直接由寄存器驱动，面积最小。

---

### 4.2 流水线接口：put / got / valid / full 与 FWFT 语义

#### 4.2.1 概念说明

PoC 的 FIFO 用的是一套统一的「流水线式」握手接口，而不是教科书里常见的 `wr_en/rd_en`。理解这一套接口是使用任何 PoC FIFO 的前提：

**写入侧（生产者驱动）**

| 信号 | 方向 | 含义 |
|------|------|------|
| `put` | in | 「我要写入」。当拍为 1 表示想把 `din` 塞进 FIFO。 |
| `din` | in | 待写入数据。 |
| `full` | out | 「满了」。为 1 时本拍写入无效。 |

写入规则：`put='1'` 且 `full='0'` 时，`din` 在该时钟上升沿被写入。

**读出侧（消费者驱动，FWFT）**

| 信号 | 方向 | 含义 |
|------|------|------|
| `valid` | out | 「当前 `dout` 上有有效数据」。 |
| `dout` | out | 当前可读数据（FWFT：`valid` 高时数据已在 `dout` 上）。 |
| `got` | in | 「我取走了」。当拍为 1 表示消费了 `dout`。 |

读出规则：只要 `valid='1'`，`dout` 就是可用的；消费方在取走的那一拍把 `got` 拉高一拍，FIFO 下一拍给出下一个数据（如果还有）。

这就是 **first-word-fall-through（FWFT，前置有效）**：数据「提前」出现在输出口，等你来取，而不是等你「请求」后才去取。它比标准 FIFO 的「先发 rdreq、下一拍才出数据」少一拍延迟，对流水线友好。

> 命名提醒：`fifo_glue` 用的是简写 `ful/vld/di/do`，`fifo_cc_got`/`fifo_ic_got` 用全写 `full/valid/din/dout`，含义一致。

#### 4.2.2 核心流程

一次「写一个、再读一个」的最小交互时序（同钟，`clk` 上升沿采样）：

```text
clk      ─┐  ┌─┐  ┌─┐  ┌─┐  ┌─┐  ┌─┐  ┌─
          └──┘ └──┘ └──┘ └──┘ └──┘ └──┘
put   ──────────┐
              写入 din=A
full  ───────────────────────（未满，保持 0）
valid ────────────（FWFT：写完后下一拍 valid 拉高）────┐
dout ────────────────────── A ──────────────────
got   ──────────────────────────────┐
                                   取走 A，valid 随后回落
```

关键点：

1. 写入当拍数据进 RAM，但 `valid` 要等到下一个时钟沿才反映出来（寄存器输出）。
2. 读侧「先看到数据、再决定取不取」，`got` 是消费脉冲，不是请求脉冲。

#### 4.2.3 源码精读

**写接口逻辑**（`fifo_cc_got.vhdl`）：

- [src/fifo/fifo_cc_got.vhdl:L307-L309](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L307-L309) — `full <= fulli; we <= put and not fulli;`。这就是写入规则的字面翻译：写使能 `we` 只有在「`put` 为 1 且未满」时才为 1，满了他强行 `put` 也会被 `not fulli` 屏蔽掉。

**读接口的 FWFT 实现**（`fifo_cc_got.vhdl`，未加 `OUTPUT_REG` 的小分支）：

- [src/fifo/fifo_cc_got.vhdl:L337-L353](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L337-L353) — 这里用一个 `Vld` 寄存器维护「输出是否有效」。核心一句是 `Vld <= (Vld and not got) or not empti;`：要么保持当前有效数据（只要没被 `got` 取走），要么在 FIFO 非空时把新数据顶上来。`re <= (not Vld or got) and not empti;` 决定何时去 RAM 取下一个字——只要当前输出无效或刚被取走，且 FIFO 非空，就发起一次读。这正是 FWFT 的「数据预先候场」逻辑。

**跨钟版对 FWFT 的文字说明**（`fifo_ic_got.vhdl`）：

- [src/fifo/fifo_ic_got.vhdl:L17-L20](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_ic_got.vhdl#L17-L20) — 注释明确写出：「FWFT mode is implemented, so data can be read out as soon as `valid` goes high. After the data has been captured, then the signal `got` must be asserted.」即 `valid` 一高即可读，取走后用 `got` 告知。

#### 4.2.4 代码实践

**实践目标**：通过追踪 `fifo_glue` 的状态机，亲手验证 FWFT 接口的微观行为。

**操作步骤**：

1. 打开 [src/fifo/fifo_glue.vhdl:L68-L106](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_glue.vhdl#L68-L106)。
2. 关注这三句输出赋值：`ful <= Full; vld <= Avail; do <= B;`（[L108-L110](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_glue.vhdl#L108-L110)）。
3. 在状态机里追踪：当 `Avail='0'` 且来一个 `put='1'` 时（[L79-L83](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_glue.vhdl#L79-L83)），`B <= di; Avail <= '1';`——数据直接进输出寄存器 `B`，下一拍 `vld` 就高。

**需要观察的现象**：

- 数据写入后落在 `B` 上，而 `do <= B`，所以下一拍 `do`（即 `dout`）直接可用，`vld` 同时拉高——这就是 FWFT。
- 没有任何「读请求」信号：FIFO 只要非空就把数据「递」到输出端。

**预期结果**：你会确认 PoC 的 FIFO 接口是「数据候场 + `got` 确认」，而不是「请求 + 应答」。后续写测试台或上层逻辑时，读侧只需盯 `valid`、用 `got` 回 ACK。

#### 4.2.5 小练习与答案

**练习 1**：在 `fifo_cc_got` 里，如果 `full='1'` 时仍然把 `put` 拉高，会发生什么？数据会被写进去吗？

**参考答案**：不会。写使能 `we <= put and not fulli`，`fulli='1'` 时 `we` 被强制为 0，RAM 不会写，该数据丢失。因此使用时必须在 `full='0'` 时才允许 `put='1'`（或接受丢弃）。

**练习 2**：FWFT 接口相对标准 FIFO（`rdreq` → 下一拍出数据）的主要好处是什么？

**参考答案**：读延迟少一拍——`valid` 一高，`dout` 上就是有效数据，可直接组合使用或在该拍用 `got` 取走；不必先发读请求再等一拍。对流水线吞吐和时序闭合都更友好。

---

### 4.3 关键 generic：DATA_REG / STATE_REG / OUTPUT_REG 与填充指示器

#### 4.3.1 概念说明

`fifo_cc_got` / `fifo_ic_got` 提供了一组 generic，让你在「面积、频率、功能」之间精细调节。先看三个布尔开关：

| generic | 默认 | 作用 |
|---------|------|------|
| `DATA_REG` | false | 为 true 时用寄存器/分布式 RAM 存数据（小而快），false 时用 ocram（BRAM）。 |
| `STATE_REG` | false | （仅 cc 版）为 true 时把 `full`/`empty` 指示做成寄存器输出，代价是多用一个比较器。 |
| `OUTPUT_REG` | false | 为 true 时在输出端再加一拍缓冲寄存器，改善时序但多一拍读延迟。 |

还有一对数值 generic 控制「填充指示器」：

| generic | 作用 |
|---------|------|
| `ESTATE_WR_BITS` | 写侧「还能再写多少」指示的位宽（0 = 不需要）。 |
| `FSTATE_RD_BITS` | 读侧「还能再读多少」指示的位宽（0 = 不需要）。 |

对应的两个输出端口：

- `fstate_rd`：关联**读时钟域**，给出 FIFO 里「至少有多少个字可读」的保守下界。
- `estate_wr`：关联**写时钟域**，给出 FIFO「至少还能再写多少个字而不溢出」的保守下界。

这两个指示器是「带粒度的水位计」：位宽越宽，水位刻得越细。源码里给了很直观的对照表（见 4.3.3）。注意它们**不能替代** `full`/`valid`，因为它们给出的是「保守（pessimistic）」估计，可能比真实值少一个。

为什么需要这两个指示器？典型场景是「提前流量控制」：上游不想等到 `full` 才停（那样太突然），而是看到 `estate_wr` 低于某个阈值就开始减速；下游同理看 `fstate_rd`。

#### 4.3.2 核心流程

`fifo_cc_got` 的核心是个环形缓冲区：用两个指针 `IP0`（输入/写指针）和 `OP0`（输出/读指针）在深度为 \(2^{A\_BITS}\) 的 RAM 里转圈。

地址位宽由 `MIN_DEPTH` 向上取整到 2 的幂：

\[
A\_BITS = \lceil \log_2(\text{MIN\_DEPTH}) \rceil
\]

实际深度为：

\[
\text{DEPTH} = 2^{A\_BITS}
\]

例如 `MIN_DEPTH=30`：\( \lceil \log_2 30 \rceil = 5 \)（因为 \(2^4=16 < 30 \le 32 = 2^5\)），所以 `A_BITS=5`，实际深度 32。

满/空判断的经典难点是：**两个指针相等时，到底是「满」还是「空」？** PoC 用两种策略应对：

- **组合策略（`STATE_REG=false`）**：额外记一个方向标志 `DF`，记住「上次是写还是读」。指针相等且上次是写 → 满；相等且上次是读 → 空。
- **寄存策略（`STATE_REG=true`）**：多花一个比较器，把 `full`/`empty` 都做成寄存器输出，时序更好但面积略大。

填充指示器的计算则更直接：

\[
d = IP0 - OP0 \quad \text{（当前有效字数）}
\]

`fstate_rd` 取 `d` 的高 `FSTATE_RD_BITS` 位；`estate_wr` 取 `d` 高位后再取反（`not`），所以「保守地少算一个」，利于综合优化。

#### 4.3.3 源码精读

**地址位宽推导**（`fifo_cc_got.vhdl`）：

- [src/fifo/fifo_cc_got.vhdl:L129](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L129) — `constant A_BITS : natural := log2ceil(MIN_DEPTH);` 这就是上面公式的代码形态。注意跨钟版 `fifo_ic_got` 用的是 `log2ceilnz`（[L109](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_ic_got.vhdl#L109)），「nz」表示「non-zero」，即使 `MIN_DEPTH=1` 也至少返回 1，避免 0 位地址。

**指针自增**（`fifo_cc_got.vhdl`，用进位链抽象）：

- [src/fifo/fifo_cc_got.vhdl:L169-L185](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L169-L185) — 实例化 `arith_carrychain_inc` 把指针 +1，复用厂商专用的快速进位链资源，比直接 `+1` 在某些器件上更省/更快。

**满/空判断的两种实现**：

- 组合版（方向标志）：[src/fifo/fifo_cc_got.vhdl:L246-L267](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L246-L267) — `DF` 记录上次操作方向，`Peq` 判指针相等，`fulli <= Peq and DF; empti <= Peq and not DF;`。
- 寄存版：[src/fifo/fifo_cc_got.vhdl:L271-L302](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L271-L302) — 用 `Ful`/`Avl` 两个寄存器分别维护，输出更干净。

**填充指示器计算**（`fifo_cc_got.vhdl`）：

- [src/fifo/fifo_cc_got.vhdl:L214-L237](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L214-L237) — 这是理解 `estate_wr/fstate_rd` 的关键。`d := std_logic_vector(IP0 - OP0);` 算出真实有效字数；满时强行 `d := (others => '1')`。然后：

```vhdl
estate_wr <= not d(d'left downto d'left-ESTATE_WR_BITS+1);  -- 取反，保守
fstate_rd <=     d(d'left downto d'left-FSTATE_RD_BITS+1);  -- 不取反
```

`estate_wr` 的取反注释写得很明白（[L229-L233](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L229-L233)）：one's complement 让结果「悲观地少一个」，但利于综合优化。

**水位对照表**（源码注释自带的例子，`fifo_cc_got.vhdl`）：

- [src/fifo/fifo_cc_got.vhdl:L38-L60](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L38-L60) — `FSTATE_RD_BITS=1` 时 `fstate_rd` 只有 0/1 两档，对应「0/2 满」和「≥1/2 满（半满）」；`FSTATE_RD_BITS=2` 时有 4 档，分别对应 0/4、1/4、2/4、3/4。可见位宽 = 水位刻度数（以 2 的幂细分）。

**DATA_REG 与 OUTPUT_REG 的分支**：

- `DATA_REG=false`（默认，用 BRAM）：[src/fifo/fifo_cc_got.vhdl:L312-L382](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L312-L382) — 实例化 `ocram_sdp`，内部再按 `OUTPUT_REG` 分「组合读」或「加一拍缓冲」两条子分支。
- `DATA_REG=true`（用寄存器/分布式 RAM）：[src/fifo/fifo_cc_got.vhdl:L384-L423](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L384-L423) — 用数组 `regfile_t` 建模，并打 `ram_style "distributed"` 属性（[L389-L390](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L389-L390)），让 XST 映射到分布式 RAM。

**跨钟版的填充指示器**（`fifo_ic_got.vhdl`）：跨钟版因为指针是格雷码、还要跨域同步，所以填充计算要先 `gray2bin` 再相减：

- [src/fifo/fifo_ic_got.vhdl:L291-L312](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_ic_got.vhdl#L291-L312) — 写侧用同步过来的读指针 `OPc` 与本地写指针 `IP0` 算 `estate_wr`；读侧用同步过来的写指针 `IPc` 与本地读指针 `OP0` 算 `fstate_rd`。这正是「每个时钟域只用本地指针 + 同步过来的对方指针」的保守计算方式，天然带保守性。

#### 4.3.4 代码实践

**实践目标**：实例化一个 `fifo_cc_got`（`D_BITS=8`、`MIN_DEPTH=30`），并解释 `estate_wr`/`fstate_rd` 的含义。这个任务直接对应官方测试台 `tb/fifo/fifo_cc_got_tb.vhdl` 的配置。

**操作步骤**：

1. 计算关键参数：`MIN_DEPTH=30` → `A_BITS = log2ceil(30) = 5` → 实际深度 `2^5 = 32`。所以你想要至少 30 深，得到的是 32 深。
2. 决定填充指示器位宽：为观察「半满」，设 `ESTATE_WR_BITS=2`、`FSTATE_RD_BITS=2`（4 档水位）。
3. 参考测试台里的实例化写法：[tb/fifo/fifo_cc_got_tb.vhdl:L89-L110](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L89-L110)。

下面是一段「示例代码」（不是项目原有代码，是按 PoC 规范仿写的最小实例化，供你放进自己的工程）：

```vhdl
-- 示例代码：实例化 fifo_cc_got，8 位宽、最小 30 深
library PoC;
use PoC.fifo.all;

entity my_fifo_wrap is
  port (
    clk, rst : in  std_logic;
    put      : in  std_logic;
    din      : in  std_logic_vector(7 downto 0);
    full     : out std_logic;
    got      : in  std_logic;
    dout     : out std_logic_vector(7 downto 0);
    valid    : out std_logic
  );
end entity;

architecture rtl of my_fifo_wrap is
  -- estate_wr/fstate_rd 暂时不接，留空
  signal estate_wr : std_logic_vector(1 downto 0);
  signal fstate_rd : std_logic_vector(1 downto 0);
begin
  fifo_inst : component fifo_cc_got
    generic map (
      D_BITS         => 8,        -- 8 位数据
      MIN_DEPTH      => 30,       -- 实际会向上取整到 32
      DATA_REG       => false,    -- 用 BRAM
      STATE_REG      => false,    -- 组合满/空判断
      OUTPUT_REG     => false,    -- 不加额外输出寄存器
      ESTATE_WR_BITS => 2,        -- 写侧 4 档水位
      FSTATE_RD_BITS => 2         -- 读侧 4 档水位
    )
    port map (
      rst       => rst,
      clk       => clk,
      put       => put,
      din       => din,
      full      => full,
      estate_wr => estate_wr,     -- 本例不使用，仅留接口
      got       => got,
      dout      => dout,
      valid     => valid,
      fstate_rd => fstate_rd      -- 本例不使用，仅留接口
    );
end architecture;
```

**需要观察的现象与 `estate_wr`/`fstate_rd` 含义解释**：

- `estate_wr`（2 位，写时钟域）：表示「FIFO 至少还能再吃进多少个字」。4 档含义（设深度 32）：`3`→至少还能写约 3/4 深度、`2`→约 2/4、`1`→约 1/4、`0`→接近 0/4（几乎满了）。上游可用它做「提前减速」。注意源码里它取了反码，所以**保守地少算一个**——它说「能写 N 个」时，真实可能能写 N+1 个，但绝不会让你溢出。
- `fstate_rd`（2 位，读时钟域）：表示「FIFO 里至少还有多少个字可读」。同样 4 档，下游可据此决定是否提前停止读取。
- 两者都是**组合输出**（[fifo_cc_got.vhdl:L33-L34](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L33-L34) 注释说明路径里含一个地址比较/减法器），若对时序敏感，可减小位宽或不用。

**预期结果**：如果你用 GHDL/NVC 编译并仿真上述包装（连同 `fifo_cc_got.files` 拉齐的依赖），向 FIFO 连续写入 30 个字，应能看到 `full` 在写满 32 个之前保持 0，`estate_wr` 从最高档逐步降到 0；读出时 `fstate_rd` 反向变化。**若你暂时没有可用的 VHDL 仿真环境，完整运行结果「待本地验证」**——但参数推导（深度 32、4 档水位）是确定的。

> 想看一个完整的「写满再读空」激励范例，可继续阅读 [tb/fifo/fifo_cc_got_tb.vhdl:L112-L120](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/fifo/fifo_cc_got_tb.vhdl#L112-L120) 的 `procWriter` 进程。

#### 4.3.5 小练习与答案

**练习 1**：把 `MIN_DEPTH` 设为 1，`fifo_cc_got` 和 `fifo_ic_got` 分别会得到多大的 `A_BITS`？为什么两者用的函数不同？

**参考答案**：`fifo_cc_got` 用 `log2ceil`，`log2ceil(1)=0`，会得到 `A_BITS=0`；`fifo_ic_got` 用 `log2ceilnz`，`log2ceilnz(1)=1`，得到 `A_BITS=1`。跨钟版必须额外保留一位（`AN := A_BITS + 1`）来区分「满」与「空」（指针相等时的二义性），所以用 non-zero 版本保证至少 1 位地址；这也是为什么跨钟 FIFO 实际容量至少为 2。

**练习 2**：`estate_wr` 为什么是 `not d(...)`（取反），而 `fstate_rd` 不取反？「保守少一个」对使用方意味着什么？

**参考答案**：取反让 `estate_wr` 成为「至少还能写多少」的下界——即使综合器优化导致差一，使用方按它来限速也绝不会溢出，安全。对使用方意味着：可以信任 `estate_wr` 作为「绝对不会写爆」的流量控制依据，但不能把它当成精确剩余容量；同理 `fstate_rd` 是「至少还能读多少」的下界。两者都不能替代 `full`/`valid` 这一拍的决定性信号。

**练习 3**：什么情况下你会把 `OUTPUT_REG` 设为 true？代价是什么？

**参考答案**：当 FIFO 输出直接驱动长组合路径或高扇出负载、时序不闭合时，设 `OUTPUT_REG=true` 在输出端加一拍寄存器，可显著改善 `dout`/`valid` 的输出时序（slack）。代价是多一拍读延迟，且多用一组数据寄存器（参考 [fifo_cc_got.vhdl:L355-L380](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L355-L380) 的 `Buf`/`Vld` 缓冲逻辑）。

---

## 5. 综合实践

**任务**：为一个假想的「传感器数据采集」场景选择并配置 FIFO，把所学串起来。

场景描述：一个 8 位 ADC 在 100 MHz 时钟域里间歇产出采样值（每若干拍一个），需要送到一个 50 MHz 时钟域的后处理模块。

**要求**：

1. **选型**：判断该用 `fifo_cc_got` 还是 `fifo_ic_got`，并说明理由。
2. **参数推导**：若希望缓冲至少 200 个采样，计算 `A_BITS` 与实际深度。
3. **接口连线**：写出 ADC 侧（写）与后处理侧（读）各应连接 `put/din/full` 还是 `got/dout/valid`，并指出 FWFT 在这里的好处。
4. **填充指示器**：若后处理模块想在「FIFO 里至少积攒到 1/4 满」时才开始批量读，应该用哪个指示器、设几位？

**参考答案要点**：

1. 选 `fifo_ic_got`：100 MHz 与 50 MHz 不相关，是跨钟域。
2. `A_BITS = log2ceilnz(200) = 8`（\(2^7=128 < 200 \le 256=2^8\)），实际深度 256。
3. ADC 侧接 `put/din/full`（写满时停采），后处理侧接 `got/dout/valid`（FWFT 让 `valid` 一高就可读，减少一拍延迟，利于 50 MHz 域跟上数据流）。注意跨钟版的读写端口分属 `clk_wr/rst_wr` 与 `clk_rd/rst_rd`。
4. 用 `fstate_rd`（读侧水位），设 `FSTATE_RD_BITS=3`（8 档，1/8 粒度，可识别 1/4 满）。看到 `fstate_rd ≥ 2`（即 ≥2/8=1/4）时启动批量读。

> 这个任务无需真正综合，重点是走通「选型 → 参数 → 接口 → 水位」这条完整决策链。若你想验证参数推导，可在本地对 `log2ceilnz(200)` 写一个最小 VHDL 断言检查（结果应为 8）。

---

## 6. 本讲小结

- PoC 的 FIFO 是一张按「时钟关系 × 功能」切分的矩阵：`fifo_glue`（2 字解耦）、`fifo_shift`（小容量移位）、`fifo_cc_got`（同钟主力）、`fifo_ic_got`（跨钟），外加 `tempput`/`tempgot`（暂存回滚）等扩展。
- 统一使用「流水线接口」：写侧 `put/din/full`，读侧 `got/dout/valid`，并采用 FWFT 语义——`valid` 高时数据已在 `dout` 上，`got` 是消费脉冲而非请求脉冲。
- `DATA_REG` 在 BRAM 与分布式 RAM 间切换；`STATE_REG` 把满/空做成寄存器输出；`OUTPUT_REG` 给输出加一拍缓冲改善时序。
- 地址位宽 `A_BITS = log2ceil(MIN_DEPTH)`（跨钟版用 `log2ceilnz`），实际深度向上取整到 2 的幂（如 30→32）。
- `estate_wr`/`fstate_rd` 是「带粒度的水位计」，分别给出写侧「至少还能写多少」、读侧「至少还能读多少」的保守下界，用于提前流量控制，但不能替代 `full`/`valid`。
- 跨钟 FIFO 用格雷码指针 + 双 FF 同步实现 CDC，填充计算要先用 `gray2bin` 把同步过来的指针转回二进制。

---

## 7. 下一步学习建议

- **继续深入 CDC**：本讲的 `fifo_ic_got` 已经用到了格雷码指针与同步器，下一讲 **u3-l6 时钟域穿越：misc/sync** 会系统讲解 `sync_Bits`/`sync_Reset`/`sync_Pulse` 等同步器，把跨钟 FIFO 背后的同步原理讲透。
- **底层存储**：想理解 `ocram_sdp` 的读-写冲突行为与厂商实例化，回看 **u3-l3 片上 RAM 抽象：ocram 家族**。
- **测试台写法**：若你想自己给 FIFO 写激励，下一单元的 **u4-2 测试台结构与编写** 会以 `fifo_cc_got_tb` 为例讲解 `for generate` 批量遍历 generic 的技巧（本讲的测试台正是用它一次跑 8 种 `DATA_REG/STATE_REG/OUTPUT_REG` 组合）。
- **扩展阅读**：阅读 [src/fifo/fifo_ic_got.vhdl:L144-L215](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_ic_got.vhdl#L144-L215) 的格雷码指针生成与同步阶段，是理解跨钟 FIFO 实现细节的最佳练习。
