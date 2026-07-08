# Skid Buffer 与 COTTC FSM 方法

## 1. 本讲目标

上一讲（u9-l2）我们确立了 ready/valid 握手的规则与本质，并指出 Skid Buffer 里的 `insert`/`remove` 就是两次 `handshake_complete`。本讲不再讲「规则是什么」，而是讲**怎么把一个满足这些规则的模块设计出来**——`Pipeline_Skid_Buffer` 正是全书用来示范设计方法的「范本模块」。

学完后，你应该能：

- 说清 **skid buffer 解决的矛盾**：为什么不能简单地给 ready/valid 接口加一拍寄存器，为什么需要「多一个缓冲寄存器让输入滑行刹车」。
- 看懂 **数据通路与控制通路的分离**：数据通路只认控制信号、不认状态编码；控制通路用状态 + 握手算出全部控制信号。
- 掌握 **COTTC 状态机设计法**（Constraints / Operations / Transformations / Transitions / Control），并能把它逐条映射到 `Pipeline_Skid_Buffer.v` 的代码结构上。
- 手动追踪一次「满状态下输入输出同时握手」（`pass` 变换）的状态/数据通路路径，并列出 EMPTY/BUSY/FULL 三态及其之间的边。

## 2. 前置知识

本讲假设你已经掌握（来自依赖讲义）：

- **u9-l2 握手规则**：`handshake_complete = (ready && valid)`；接口内禁组合环；内部状态只在握手完成拍改变；`insert`/`remove` 是两次 `handshake_complete`。
- **u6-l1 Register 家族**：`Register` 是带 `clock_enable`/`clear` 的同步寄存器，用「最后赋值胜出」(last-assignment-wins) 让 `clear` 自然优先于 `clock_enable`。本模块会实例化它 4 次。
- **u3-l1 赋值与三元**：组合块用阻塞 `=`、时钟块用非阻塞 `<=`；链式三元 + 阻塞赋值天然就是有限状态机（FSM）的「最后赋值胜出」范式。

还需两个数字电路常识：

- **流水线寄存器（pipeline register）**：插在组合逻辑中间的触发器，把一条长组合路径切成两段短路径，以提高时钟频率，代价是多一拍延迟。
- **组合路径**：从输入到输出、不经过任何寄存器的纯逻辑通路；跨接口的组合路径会拉长关键路径甚至成环（见 u9-l2）。

## 3. 本讲源码地图

本讲精读两个文件，一抽象一具体：

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| `fsm.html` | FSM 实现**方法正文** | 给出 COTTC 五步设计法、三种 next-state 组合方式、DFA 安全性论证 |
| `Pipeline_Skid_Buffer.v` | 方法的**范本实现** | 把 COTTC 五步逐条落地成代码；提供 EMPTY/BUSY/FULL 状态图与 load/flow/fill/flush/unload/dump/pass 七种数据通路变换 |

此外会反复用到 `Register`（见 u6-l1）作为唯一的寄存器构件。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**Skid Buffer 解决的矛盾**、**数据/控制分离**、**COTTC FSM 设计法**。

### 4.1 Skid Buffer 解决的矛盾

#### 4.1.1 概念说明

很多场合我们需要在一段 ready/valid 连接中间插一拍流水线寄存器——为了把长组合路径切断、提升频率，或为了让发送端能「先发一笔不等回复」（让通信与计算重叠）。但**给握手接口加寄存器远比给数据加寄存器难**。

作者在正文里把困难讲得很直白。先想象一个能在两侧各做一次 ready/valid 握手、收一笔数据再发出去的小盒子。理想情况下，输入输出两侧**同一拍并发**工作：一拍内，输入侧把新数据写进寄存器，输出侧同时把这个寄存器读走——这样带宽最大。可一旦输出侧这一拍不取数据，输入侧这一拍就**绝不能**也完成握手，否则会在寄存器被读走之前就把它覆盖掉。于是「输入侧该不该 ready」就不得不**当拍**知道「输出侧这拍有没有在 transfer」——这就形成了一条输入到输出的**直接组合连接**，恰恰不是我们要的流水线。作者的原话点破了死结：

> *If we could connect both interfaces directly, and not affect timing or concurrency, we wouldn't need pipelining in the first place!*

化解办法是**再加一个缓冲寄存器**：当输入侧在 transfer、输出侧没 transfer、而主寄存器里已经有数据时，用这个缓冲寄存器把当拍进来的数据接住；下一拍输入侧再宣布自己不 ready，数据就不会丢。可以把这个缓冲寄存器理解成让输入侧得以「**滑行刹车**」（skid to a stop），而不是必须立即停下——这正是 *skid buffer* 名字的由来。

正文还给出一个可选的 **Circular Buffer Mode（CBM，循环缓冲模式）**：把 `CIRCULAR_BUFFER` 参数设为非零值后，输入侧握手**永远可以完成**——即使缓冲里已有数据没人取走，也直接丢弃最旧的那笔、换成新进的。普通模式缓存的是「**最早**」的数据（停顿时先堵再停），CBM 缓存的是「**最新**」的数据。这其实就是一个**两入口的循环缓冲**。

#### 4.1.2 核心流程

把上面的矛盾与化解整理成一张因果图：

```
目标: 在 ready/valid 连接中间插一拍流水线, 且两侧能并发握手 (满带宽)

矛盾:
  要并发 -> 输入 ready 必须当拍知道输出是否在 transfer
          -> 输入到输出的组合连接 -> 不是流水线 (自相矛盾)

化解 (skid buffer):
  多一个 buffer 寄存器接住"输入在传、输出不传、主寄存器已满"那拍的数据
  -> 输入可以"滑行刹车": 下一拍再宣布不 ready, 数据不丢
  -> 两侧之间只剩寄存器, 无组合路径

两种行为:
  普通 mode  : 缓存"最早"数据; 满了就强制输入输出交替 (不能同拍都握手)
  CBM (循环) : 缓存"最新"数据; 满了仍可同拍又收又发 (满载吞吐, 2 拍延迟)
```

普通模式下，「满了」就逼着输入输出握手**交替**进行（不能在满状态下同拍既收又发）；CBM 则打破了这条限制。

#### 4.1.3 源码精读

**模块定位与端口。** `Pipeline_Skid_Buffer` 自我描述为「把 ready/valid 握手两侧解耦、消除输入到输出组合路径、从而把通路流水线化」的最小构建块，也能当两入口循环缓冲用：

[Pipeline_Skid_Buffer.v:4-18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L4-L18) —— 定位：最小的 Pipeline FIFO Buffer（仅两入口），用于在两侧之间流水线化握手、并发与改善时序，而不用于平滑速率失配；又名 Carloni Buffer。

模块端口很标准：一对输入握手（`input_valid`/`input_ready`/`input_data`）、一对输出握手（`output_valid`/`output_ready`/`output_data`），加 `clock`/`clear`，以及参数 `WORD_WIDTH` 与 `CIRCULAR_BUFFER`：

[Pipeline_Skid_Buffer.v:106-124](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L106-L124) —— 端口定义；`CIRCULAR_BUFFER` 默认 0（普通模式），非零启用循环缓冲模式。

**矛盾与化解（正文）。** 作者在注释里完整推演了前述矛盾与 skid 化解：

[Pipeline_Skid_Buffer.v:46-80](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L46-L80) —— 从「两侧理想并发」推到「输入 ready 必须当拍依赖输出 transfer」的组合连接矛盾，再给出「加 buffer 寄存器让输入滑行刹车」的化解。

**循环缓冲模式（正文）。** CBM 的语义与它对「满状态下能否同拍收发」的影响：

[Pipeline_Skid_Buffer.v:82-104](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L82-L104) —— 普通 vs CBM：前者缓存最早值、满则交替握手；后者缓存最新值、满仍可同拍又收又发，满载吞吐、2 拍延迟，因为 `input_ready` 不再依赖缓冲空满状态，也不依赖输出握手状态。

**资源开销。** 一个 64 位连接的 skid buffer 只要 128 个数据寄存器（两个 64 位寄存器）加 4–9 个 FSM/接口寄存器：

[Pipeline_Skid_Buffer.v:399-402](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L399-L402) —— 64 位连接：128 个缓冲寄存器 + 4–9 个 FSM/接口寄存器（随状态编码而变），易达高速。

#### 4.1.4 代码实践

**实践目标**：亲手追踪「上电后第一笔数据如何进入输出寄存器」，体会 skid buffer 在 EMPTY 状态下的默认数据通路。

**操作步骤**：

1. 上电/`clear` 后状态为 EMPTY（见后文 [L376](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L373-L385) 的 `RESET_VALUE (EMPTY)`）。此时三个控制信号的默认值被刻意设成「让第一笔输入直接进输出寄存器」：

[Pipeline_Skid_Buffer.v:143](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L143) 与 [Pipeline_Skid_Buffer.v:160-161](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L160-L161) —— EMPTY 初值：`data_buffer_wren=0`（不加载缓冲）、`data_out_wren=1`（接受数据）、`use_buffered_data=0`（用输入而非缓冲）。

2. 第一拍输入侧握手完成（`input_valid && input_ready` 都为 1）。此时变换为 `load`（EMPTY + insert），由后文 [L392](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L391-L395) 得 `data_out_wren=1`、其余两个控制信号维持默认 0。
3. 于是 `selected_data = input_data`（因 `use_buffered_data=0`，见 [L164-166](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L164-L166)），`data_out_reg` 把它存下，下一拍 `output_data` 即生效、状态变 BUSY。

**需要观察的现象**：第一笔数据只动了**输出寄存器**，缓冲寄存器纹丝不动；状态从 EMPTY 跳到 BUSY。

**预期结果**：第一笔数据一拍进入输出寄存器并立即可被下游取走，缓冲寄存器此时闲置——这正是 EMPTY 默认值的设计意图。逐拍验证需自行搭 testbench，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：能不能不要缓冲寄存器，只留一个输出寄存器，靠「输入 ready 组合跟随输出 ready」来实现满带宽并发？

**答案**：不能。要满带宽并发，输入侧必须**当拍**知道输出侧这拍有没有 transfer，才能决定自己要不要 ready——这就构成输入到输出的组合连接，违背了流水线的初衷（作者原话：若能直连又不影响时序与并发，根本不需要流水线）。缓冲寄存器的意义正是用一拍延迟换掉这条组合路径。

**练习 2**：普通模式下 skid buffer「满了」之后，输入输出握手还能不能同拍都完成？CBM 下呢？

**答案**：普通模式不能——满了（FULL）时 `input_ready` 被拉低，输入侧无法握手，于是输入输出被迫**交替**。CBM 下能——`input_ready` 恒为 1，满状态下仍可同拍 `insert`+`remove`（即后文的 `pass` 变换），从而满载吞吐、2 拍延迟。

---

### 4.2 数据通路与控制通路的分离

#### 4.2.1 概念说明

`Pipeline_Skid_Buffer.v` 用两行注释把整份文件劈成两半：`Data Path` 与 `Control Path`。这是本书反复强调的设计纪律（参见 u4-l1 的「处理/控制/接口三分法」），在这里体现为：

- **数据通路（Data Path）** 只认控制信号（写使能、数据选择），**完全不关心**当前状态是什么、状态怎么编码。它就是「两个寄存器 + 一个二选一」。
- **控制通路（Control Path）** 负责所有「在什么状态下、根据握手信号、该做哪种变换、该发哪些控制信号、下一状态是谁」。状态编码、状态转移全部集中在这里。

这样切分的好处：数据通路可以独立重定时（retiming）到下游逻辑里去改善时序；而控制逻辑换了状态编码（比如从二进制改 one-hot）数据通路一行都不用改。作者特意把输出汇成**单个** `data_out` 寄存器，而不是两个并列输出寄存器再二选一，正是为了减少一级寄存器后 mux、方便重定时。

#### 4.2.2 核心流程

数据通路与控制通路的分工：

```
控制通路 (Control Path):
  输入: 当前 state + 两侧握手信号 (valid/ready)
  输出: 三个控制信号 -> 数据通路
        data_out_wren      (输出寄存器写使能)
        data_buffer_wren   (缓冲寄存器写使能)
        use_buffered_data  (输出寄存器数据源选择)
  副产: input_ready / output_valid / state_next (寄存后输出)

数据通路 (Data Path):
  输入: input_data + 三个控制信号
  实现: buffer 寄存器 <- input_data        (受 data_buffer_wren 门控)
        selected_data   = use_buffered_data ? buffer_out : input_data
        输出寄存器      <- selected_data    (受 data_out_wren 门控)
  输出: output_data
```

关键点：数据通路里**没有任何**对 `state` 的引用，它只是被动地按控制信号搬数据。

#### 4.2.3 源码精读

**数据通路：两个寄存器 + 一个二选一。** 缓冲寄存器 `data_buffer_reg` 直接存 `input_data`，受 `data_buffer_wren` 门控：

[Pipeline_Skid_Buffer.v:143-158](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L143-L158) —— 缓冲寄存器：写使能 `data_buffer_wren` 初值 0（EMPTY 时不加载），存 `input_data`。

输出寄存器 `data_out_reg` 的数据源由 `use_buffered_data` 在「输入数据」与「缓冲数据」间二选一，受 `data_out_wren` 门控：

[Pipeline_Skid_Buffer.v:160-180](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L160-L180) —— 输出寄存器：`selected_data` 组合选择，`data_out_wren` 初值 1（EMPTY 时接受数据）；注释说明为何汇成单输出寄存器（少一级 mux、更易重定时），并把控制信号初值对齐 EMPTY 状态。

注意整段数据通路**没有一个** `state` 字样——数据/控制分离在这里是字面意义上的。

**控制通路：集中所有状态与握手逻辑。** 控制段从注释 `Control Path` 开始，统管状态编码、接口约束、变换、转移与控制输出：

[Pipeline_Skid_Buffer.v:182-186](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L182-L186) —— 控制段开场：把控制路径分离出来，使数据通路无需了解当前状态及其编码。

**控制信号不被寄存。** 最后三个控制输出是组合逻辑、直接喂给数据通路里的寄存器写使能/选择端，故无需再打一拍：

[Pipeline_Skid_Buffer.v:387-395](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L387-L395) —— 控制输出（`data_out_wren`/`data_buffer_wren`/`use_buffered_data`）由各变换 OR 派生，不再寄存，因其终点就是数据通路里的寄存器。

#### 4.2.4 代码实践

**实践目标**：在源码里把每个信号归类成「数据通路」或「控制通路」，亲手验证二者真的互不引用。

**操作步骤**：

1. 打开 `Pipeline_Skid_Buffer.v`，画出一条竖线把 L128（`Data Path` 注释）到 L180（输出寄存器实例结束）划为数据通路，把 L182（`Control Path`）到 L395 划为控制通路。
2. 在数据通路段内（L128–L180）搜索 `state`：确认**零次**出现。
3. 列一张归类表：

| 信号 | 归属 | 说明 |
|------|------|------|
| `input_data` / `output_data` | 数据 | 搬运的载荷 |
| `data_buffer_out` / `selected_data` | 数据 | 内部数据连线 |
| `data_buffer_wren` / `data_out_wren` / `use_buffered_data` | 控制→数据的接口 | 由控制段算出，喂给数据段 |
| `state` / `state_next` | 控制 | 状态机 |
| `insert` / `remove` | 控制 | 握手完成（操作） |
| `load`/`flow`/…/`pass` | 控制 | 数据通路变换 |
| `input_ready` / `output_valid` | 控制（寄存输出） | 接口约束 |

**需要观察的现象**：数据通路段完全由控制段的三个写使能/选择信号驱动；控制段不直接搬任何 `input_data`（除了把它接给缓冲寄存器的 `data_in`）。

**预期结果**：能口述「换了状态编码，数据通路一行不改；换了数据位宽，控制逻辑一行不改」。这是一个阅读/归类型实践，无需仿真。

#### 4.2.5 小练习与答案

**练习 1**：作者为什么把输出汇成**单个** `data_out` 寄存器，而不是用两个并列输出寄存器再二选一？

**答案**：单输出寄存器避免了「寄存器之后再加一级由两路数据驱动的 mux」（更多布线、更大延迟），也更容易把这一级寄存器重定时进下游逻辑里去改善时序。参见 [L133-136](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L133-L136)。

**练习 2**：控制信号 `data_out_wren` 等为什么不再额外打一拍寄存？

**答案**：因为它们的终点本就是数据通路里寄存器的写使能/选择端——已经落在寄存器上了，再寄存一次只会徒增一拍延迟。参见 [L387-389](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L387-L389)。

---

### 4.3 COTTC FSM 设计法

#### 4.3.1 概念说明

前面两节讲了 skid buffer「是什么、为什么、怎么切分」。本节讲**怎么系统地把它的控制逻辑设计出来**——这就是 `fsm.html` 提出的 **COTTC** 方法。

一个朴素写状态机的办法是：画完状态图后，手工枚举「每个状态、在每种输入组合下，该去哪个状态」。对 skid buffer 来说这不可行：3 个状态，每个状态接两个接口、每接口两个信号（valid/ready），共 4 个二值信号，每个状态有

\[
2^{4} = 16
\]

种输入组合，3 个状态共 \(3 \times 16 = 48\) 条可能转移。手工枚举 48 条、再合并等价项、再剔除非法项，既繁琐又易错。

COTTC 的思路反过来：**别枚举转移，改声明约束**。先把「在什么状态下允许 insert/remove」的约束写成逻辑，再据此定义数据通路变换（transformations），变换一旦定义好，状态转移和数据通路控制就几乎「免费」得到了。五步分别是：

1. **C — Constraints（约束）**：定义输出的约束（如某信号只能在某些状态下有效）。
2. **O — Operations（操作）**：定义数据通路的基本操作，写成控制输入/输出的布尔组合。
3. **T — Transformations（变换）**：定义数据通路的变换——某操作在给定状态下意味着什么。
4. **T — Transitions（转移）**：定义状态转移——给定某变换，下一状态是谁。
5. **C — Control（控制）**：定义控制输出——哪些变换会拉起某个输出（对变换做简单布尔逻辑）。

`fsm.html` 还给出三种计算 next-state 的组合方式（链式 mux / 并行树 OR 归约 / one-hot），并论证：对 DFA（确定性有限自动机）而言，「链式 mux 强加优先级漏掉并发转移」「并行树把多个 match 合成无意义状态」这些隐患**都不会发生**——因为 DFA 里同一组（当前状态 + 当前信号）不可能导向多个不同状态；要在 COTTC 里制造冲突，你得**显式**写两个测试同一变换却选不同下一状态的 checker，而那样冲突会肉眼可见。

#### 4.3.2 核心流程

把 COTTC 五步与 skid buffer 的状态图串起来：

```
状态: EMPTY, BUSY, FULL  (三态; CBM 不引入新状态, 是精化期参数)

C 约束:  input_ready  = (state_next != FULL) || CBM    [满则不收, CBM 例外]
         output_valid = (state_next != EMPTY)          [空则无输出]
         -> 顺带剪掉大量非法转移, 且保证两接口互不组合依赖

O 操作:  insert = input_valid  && input_ready          [输入侧握手完成]
         remove = output_valid && output_ready         [输出侧握手完成]
         (正是 u9-l2 的 handshake_complete × 2)

T 变换:  load/fill/flow/unload/flush/dump/pass          [操作 × 状态 = 命名边]
         每个变换 = (state == ?) && (insert/remove 的某种组合)

T 转移:  load->BUSY, flow->BUSY, fill->FULL,
         flush->BUSY, unload->EMPTY, dump->FULL, pass->FULL
         (用"链式三元 + 最后赋值胜出"实现 fsm.html 的方法1)

C 控制:  data_out_wren     = load||flow||flush||dump||pass
         data_buffer_wren  = fill||dump||pass
         use_buffered_data = flush||dump||pass
         (对变换做 OR, 即 fsm.html 的控制输出步)
```

三态与七条边（普通模式 5 条，CBM 多 2 条）来自源码里的状态图：

```
              /--\ +- flow            (BUSY 自环)
       load   |  v   fill
 -----  +    -----  +    -----         (CBM)
|Empty| ---> |Busy| ---> |Full| ---\ +  dump
|     | <--- |    | <--- |    | <--/ +- pass
 -----   -   -----   -   -----
       unload       flush
```

整理成表：

| 变换 | 触发条件 | 状态转移 | 模式 |
|------|----------|----------|------|
| `load` | EMPTY + insert | EMPTY → BUSY | 普通 |
| `unload` | BUSY + remove | BUSY → EMPTY | 普通 |
| `fill` | BUSY + insert | BUSY → FULL | 普通 |
| `flush` | FULL + remove | FULL → BUSY | 普通 |
| `flow` | BUSY + insert + remove | BUSY → BUSY | 普通 |
| `dump` | FULL + insert (+CBM) | FULL → FULL | 仅 CBM |
| `pass` | FULL + insert + remove (+CBM) | FULL → FULL | 仅 CBM |

注意：CBM **不引入新状态**（仍是三态），它只是精化期参数、改变了 FULL 状态下允许的变换集合。

#### 4.3.3 源码精读

**方法正文：COTTC 五步。** `fsm.html` 把五步列为有序清单：

[fsm.html:22-31](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/fsm.html#L22-L31) —— 定义状态转移图后，按「输出约束 → 基本操作 → 数据通路变换 → 状态转移 → 控制输出」五步增量搭建 FSM。

**DFA 安全性与 COTTC 得名。** 正文随后论证「链式 mux 强加优先级」「并行树合成无意义状态」等隐患对 DFA 不成立，并在这一段首次把五步缩写为 **COTTC**：

[fsm.html:64-75](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/fsm.html#L64-L75) —— 这些隐患对 DFA 不可能发生；要在 constraints/operations/transformations/transitions/control (COTTC) 方案里制造冲突，必须显式写两个测试同一变换却选不同下一状态的 checker，冲突肉眼可见。

**三种 next-state 组合方式。** 正文给出三种实现，skid buffer 用的是第一种（链式 mux）：

[fsm.html:39-44](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/fsm.html#L39-L44) —— 方法 1：一串 mux，要么传递当前/已选状态、要么替换为新选状态；易读，依赖综合器化简链上逻辑，N 状态需 log₂N 位。

[fsm.html:46-53](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/fsm.html#L46-L53) —— 方法 2：并行 mux 树输出「选中下一状态或 0」，再加一位「无任何 mux 输出时回传当前状态」，全部 OR 归约得最终下一状态；关键路径更短但更费手工。

[fsm.html:55-60](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/fsm.html#L55-L60) —— 方法 3：独立并行 checker 各自置/清一个触发器，每个触发器代表一个状态（one-hot）；最并行、逻辑路径最短，N 状态需 N 位。

下面把 COTTC 五步逐条对到 `Pipeline_Skid_Buffer.v`。

**第 1 步 C（约束）—— 剪掉非法转移。** 作者把这段代码称作「关键的一小段」，因为它把「输入只能在非满时 insert、输出只能在非空时 remove」的约束落了地，并用 `state_next`（寄存过的下一状态）来算，从而保证一个接口的当前状态**不依赖**另一接口的当前状态（无组合路径）：

[Pipeline_Skid_Buffer.v:271-286](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L271-L286) —— 约束：输入只能在非满时 insert、输出只能在非空时 remove（CBM 下输出满时也能 insert）；用 `state_next` 算是为了得到寄存过的输出，且这段代码隐含 skid buffer 的根本假设——一接口的当前状态不得依赖另一接口的当前状态。

约束直接生成两个接口的握手信号（都经 `Register` 打一拍，复位初值对齐 EMPTY）：

[Pipeline_Skid_Buffer.v:291-303](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L291-L303) —— `input_ready = (state_next != FULL) || (CIRCULAR_BUFFER != 0)`，CBM 下恒为 1；寄存输出，复位初值 1（EMPTY 时接受数据）。

[Pipeline_Skid_Buffer.v:307-319](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L307-L319) —— `output_valid = (state_next != EMPTY)`，寄存输出，复位初值 0。

**第 2 步 O（操作）—— 两次 handshake_complete。** 这一步承接 u9-l2，定义两个基本操作：

[Pipeline_Skid_Buffer.v:325-331](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L325-L331) —— `insert = (input_valid && input_ready)`、`remove = (output_valid && output_ready)`，即两侧各自的握手完成。

**状态编码。** 三态用二进制编码（CAD 工具可重编码）：

[Pipeline_Skid_Buffer.v:258-269](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L258-L269) —— `STATE_BITS=2`，`EMPTY/BUSY/FULL` 分别为 0/1/2；状态 3 不可达（不存在「只有缓冲寄存器有数据」的情形），不做错误处理。

**第 3 步 T（变换）—— 命名七条边。** 每个变换是「某状态 × 某种 insert/remove 组合」，恰好描述状态图的一条边：

[Pipeline_Skid_Buffer.v:342-358](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L342-L358) —— 七个变换的声明与定义：`load/flow/fill/unload/flush` 为普通模式 5 条边，`dump/pass` 额外要求 `CIRCULAR_BUFFER != 0`（CBM 专属）。注释里逐条写明每个变换对数据通路做了什么。

**第 4 步 T（转移）—— 链式 mux（方法 1）。** 用「链式三元 + 最后赋值胜出」由变换算出 `state_next`，正是 u3-l1 的 FSM 范式，也是 `fsm.html` 方法 1：

[Pipeline_Skid_Buffer.v:363-371](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L363-L371) —— 每行一个三元：对应变换成立则选其目标状态，否则保留当前/已选状态；最后一行落定的就是 `state_next`。

`state_next` 再经一个 `Register` 存成 `state`（复位初值 EMPTY）：

[Pipeline_Skid_Buffer.v:373-385](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L373-L385) —— 状态寄存器 `state_reg`，`clock_enable` 恒 1，复位到 EMPTY。

**第 5 步 C（控制）—— 变换 OR 出控制信号。** 三个数据通路控制信号由变换 OR 派生（这里用的就是 `fsm.html` 方法 2 里提到的 OR 归约思想，只是归约的是变换而非 next-state）：

[Pipeline_Skid_Buffer.v:391-395](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L391-L395) —— `data_out_wren = load||flow||flush||dump||pass`；`data_buffer_wren = fill||dump||pass`；`use_buffered_data = flush||dump||pass`。

至此，COTTC 五步与代码一一对应：约束步剪掉非法转移、操作步定义握手完成、变换步命名状态图的边、转移步用链式 mux 算下一状态、控制步用 OR 算出数据通路控制信号。**没有任何一步需要手工枚举那 48 条转移。**

#### 4.3.4 代码实践

**实践目标**：追踪一次「满状态下输入输出同时握手」的完整路径（即 `pass` 变换），并据此回答它为何**只在 CBM 下**成立；同时列出三态及其边。

**操作步骤**：

1. **确认前提**：启用 CBM（`CIRCULAR_BUFFER != 0`），当前 `state == FULL`，输入侧 `input_valid=1`、输出侧 `output_ready=1`。
2. **算两个操作（O 步）**：
   - `input_ready`：由 [L301](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L291-L303)，CBM 下 `(state_next != FULL) || 1` = 1，故 `insert = 1 && 1 = 1`。
   - `output_valid`：FULL 时为 1，故 `remove = 1 && 1 = 1`。
3. **命中变换（T 步）**：由 [L357](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L350-L358)，`pass = (FULL) && insert && remove && CBM = 1`，其余六个变换因状态或 insert/remove 组合不符全为 0。
4. **算下一状态（T 步）**：由 [L370](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L363-L371)，`state_next = pass ? FULL : …` = **FULL**（自环，保持满）。
5. **算控制信号（C 步）**：由 [L392-394](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L391-L395)，`data_out_wren=1`、`data_buffer_wren=1`、`use_buffered_data=1`。
6. **回到数据通路看效果**（参见 [L143-L180](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L143-L180)）：
   - `data_buffer_reg` 写使能为 1、`data_in = input_data` → **缓冲寄存器存入「新输入」**。
   - `use_buffered_data=1` → `selected_data = data_buffer_out`（**旧缓冲内容**）。
   - `data_out_reg` 写使能为 1、`data_in = selected_data` → **输出寄存器被「旧缓冲」覆盖**。
   - 输出寄存器**原本的内容**被下游 `remove` 握手取走（消费）。

**需要观察的现象**：整条两级寄存器链同时前移一格——新输入进缓冲、旧缓冲进输出、旧输出被消费；状态维持 FULL，每拍完成一笔传输（满载吞吐）。

**列出三态及其边**（见 4.3.2 表格）：EMPTY/BUSY/FULL 三态；普通模式 5 条边（load、unload、fill、flush、flow），CBM 多 2 条（dump、pass，均为 FULL 自环）。本实践追踪的 `pass` 正是 CBM 专属的那条 FULL 自环。

**预期结果 / 关键结论**：「满状态下同拍又收又发」这条路径对应 `pass` 变换，**仅当 `CIRCULAR_BUFFER != 0`** 时存在。普通模式下 FULL 时 `input_ready = (state_next != FULL)` 会被拉低，`insert` 不可能为 1，于是 `pass` 永不命中——满状态强制输入输出交替。这正是 4.1 节 CBM 与普通模式差异的代码层落点。逐拍时序验证需自行搭 testbench，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：状态 3（`EMPTY/BUSY/FULL` 之外的编码）为什么不存在、也不做错误处理？

**答案**：因为不存在「只有缓冲寄存器有数据、输出寄存器空」的情形——数据总是先进输出寄存器（`load`），只有输出寄存器已有数据且又来新数据时才会进缓冲寄存器（`fill`）。所以状态 3 不可达，作者选择不处理（也可加一个错误标志，见 [L265-266](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Skid_Buffer.v#L260-L266)）。

**练习 2**：`state_next` 的计算用的是 `fsm.html` 三种方法里的哪一种？为什么这种方式对 DFA 安全？

**答案**：用的是方法 1（链式 mux，逐行三元 + 最后赋值胜出）。对 DFA 安全是因为 DFA 里同一组（当前状态 + 当前信号）不可能同时命中多条导向**不同**下一状态的边——链式 mux 的优先级只会在「命中同一边」时生效，不会漏掉并发转移；要制造真冲突，得显式写两个测试同一变换却选不同下一状态的 checker（见 [fsm.html:64-75](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/fsm.html#L64-L75)）。

**练习 3**：为什么说 COTTC「不用手工枚举 48 条转移」？

**答案**：因为约束步（C）已经把「输入只能在非满 insert、输出只能在非空 remove」写成逻辑，自动剪掉了大量非法转移；变换步（T）又把合法转移按「状态 × 操作组合」命名成 7 条边；转移步和控制步都从这 7 个变换派生。设计师只需声明 7 个变换的布尔条件，48 条原始转移的筛选与合并由逻辑表达式隐式完成。

---

## 5. 综合实践

用 COTTC 五步法，给 `Pipeline_Skid_Buffer` 增加一个一位状态输出 `buffer_full`（当两个寄存器都持有数据，即 `state == FULL` 时拉高），把本讲三块内容（矛盾动机、数据/控制分离、COTTC 方法）串起来。

**任务**：

1. 先确认这是「控制输出」而非「数据」——它属于控制通路，由 `state` 派生，与数据通路无关（呼应 4.2 的分离纪律）。
2. 按 COTTC 五步走一遍（示例代码，非项目原有文件，仅做设计演练）：

```verilog
// 示例代码 —— 用 COTTC 法增加 buffer_full 输出 (设计演练)
// C 约束: buffer_full 只在 FULL 状态有效 (本身就是一个输出约束)
// O 操作: 复用已有 insert/remove, 无需新操作
// T 变换: 复用已有 load/.../pass, 无需新变换
// T 转移: 状态机不变, FULL 仍是 FULL
// C 控制: 新输出由"当前状态"直接派生
assign buffer_full = (state == FULL);
```

3. 核对四点：
   - 它满足 4.1 的动机：`buffer_full` 只是把内部 FULL 状态暴露给外部，不改变 skid buffer 任何握手行为。
   - 它遵守 4.2 的分离：`buffer_full` 在控制通路里，数据通路一字未动。
   - 它是 COTTC 第 5 步（C，控制输出）的一个最简实例——「某状态 → 某输出」。
   - 它没有引入两个接口之间的组合路径：`buffer_full` 只依赖 `state`（寄存过的），不依赖对当拍握手信号。
4. 进阶思考：若要求 `buffer_full` 在 **CBM 的 `pass`/`dump` 期间也保持高**，你的 `assign` 还成立吗？（提示：`pass`/`dump` 都让状态留在 FULL，所以 `(state == FULL)` 天然成立——这正是「变换决定下一状态、下一状态决定输出」的连锁效果。）

**预期结果**：能说清「新增一个状态派生输出，只需走 COTTC 的 C 步（控制输出），其余四步不动」，体会 COTTC 把设计拆成可独立推理的五步的价值。综合验证需自行在 CAD 工具中完成，**待本地验证**。

## 6. 本讲小结

- **Skid buffer 化解握手流水线矛盾**：直接给 ready/valid 加寄存器会让传输「两拍起、两拍停」，且满带宽并发会逼出输入到输出的组合路径；多一个缓冲寄存器让输入「滑行刹车」，用一拍延迟换掉组合路径。
- **两种行为**：普通模式缓存「最早」数据、满则强制输入输出交替；CBM 缓存「最新」数据、满仍可同拍收发（满载吞吐、2 拍延迟），且 CBM 不引入新状态。
- **数据/控制分离**：数据通路（两寄存器 + 一个二选一）只认三个控制信号、完全不引用 `state`；控制通路集中所有状态编码、握手、变换与转移；单输出寄存器便于重定时。
- **COTTC 五步**：Constraints（剪非法转移）→ Operations（insert/remove）→ Transformations（命名 7 条边）→ Transitions（链式 mux 算下一状态）→ Control（OR 出数据通路控制信号）；让设计师声明约束而非手工枚举 48 条转移。
- **三态七边**：EMPTY/BUSY/FULL；普通模式 5 条边（load/unload/fill/flush/flow），CBM 多 2 条 FULL 自环（dump/pass）。
- **`pass` 变换**：满状态 + 同拍收发，仅 CBM 存在；效果是两级寄存器链同时前移一格、维持满载吞吐。

## 7. 下一步学习建议

本讲把 skid buffer 作为 COTTC 的范本拆透。后续可沿三条线展开：

- **缓冲家族**：下一讲 u10-l2《Half Buffer 与延迟握手》会讲 `Pipeline_Half_Buffer` 如何用「延迟握手」实现迭代计算控制，并与本讲 skid buffer 对比吞吐与适用场景；u12-l1 会把缓冲扩展到 FIFO/Credit/Stall_Smoother 等不同深度与流控策略。
- **分流合流**：u12-l2/3 的 Fork/Join/Branch/Merge 复用本讲的状态机思路，把「单接口握手」推广到「多接口会合」，可对照本讲 COTTC 法阅读。
- **方法迁移**：`fsm.html` 的方法 2（并行树 OR 归约）与方法 3（one-hot）本书在其他 FSM 模块里都有落地；读 `Arbiter_Round_Robin`（u11-l1）时，可试着用 COTTC 五步重新推导它的状态转移，检验你是否真的掌握了这套设计法。
