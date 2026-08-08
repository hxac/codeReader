# 中断处理与双寄存器组

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ZipCPU 为什么**不需要中断向量表**，以及它用什么机制替代向量表。
- 完整描述一次中断/陷阱/异常发生时，**硬件自动完成的动作序列**：GIE 清零、切换寄存器组、把原因记录进 `uCC`、在「上次离开处」恢复执行。
- 区分「响应中断」（靠硬件切组，零保存开销）与「任务切换」（用软件把用户上下文存进内存）这两件不同的事。
- 读懂 supervisor 主循环如何用 `RTU` + 读 `uCC` + 查 `icontrol` 来构造出传统向量中断的效果。
- 理解可选中断控制器 `icontrol` 如何把最多 15 路外部中断合并成 CPU 唯一的一条中断线。

## 2. 前置知识

本讲承接 **u2-l1（ISA 概览：寄存器组与状态寄存器）**，默认你已经知道：

- ZipCPU 有 **两套寄存器组**：supervisor 组（编号 0–15）与 user 组（编号 16–31），各 16 个 32 位通用寄存器。
- `CC`（状态寄存器，R14）的 **bit 5 是 GIE（全局中断使能）位**，它同时充当寄存器地址的「第 5 位」——也就是说 GIE 这一位就决定了当前用哪套寄存器组。
- `CC` 的低 16 位才有意义：bit 0–3 是 Z/C/N/V 标志，bit 4–7 是 SLEEP/GIE/STEP/BREAK 控制位，bit 8–15 是非法指令、陷阱、总线错、除零等异常状态位。
- 复位即进入 supervisor 模式（中断关）；用户模式靠 `RTU` 指令进入（中断开）。

本讲要回答的核心问题是：**既然没有中断向量表，那中断来了 CPU 去哪儿执行？现场怎么保存？** 答案全部围绕「双寄存器组」展开。

如果你还没读过 u2-l1，建议先回去把上面四点弄清楚再继续。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范。本讲重点读其中的 **Interrupt Handling**、**Traditional Interrupt Handling**、**Context Switch**、**Interrupt Controller(s)** 四个章节，以及 Operating Modes / Status Register 中关于 GIE 的描述。 |
| [README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md) | 项目总览。第 34 行一句话点明「无中断向量、靠双寄存器组」的设计哲学。 |
| [rtl/peripherals/icontrol.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v) | 可选的中断控制器外设。把多路外部中断合并成单条中断线送给 CPU，是「软件构造向量中断」的关键拼图。 |
| [bench/asm/simtest.s](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simtest.s) | 模拟器测试程序。其中的 `traptest` / `testbench` 用真实汇编演示了 `RTU` 往返与读 `uCC` 判因，是本讲代码实践的事实依据。 |

> 说明：`icontrol.v` 与 `simtest.s` 不在最初给定的 `source_files` 清单里，但它们直接服务于本讲的两个学习目标（icontrol 的作用、中断进入/返回的切换），且经 `Grep` 确认存在，故纳入讲解。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1** ZipCPU 的中断模型：为什么没有向量表
2. **4.2** 中断进入与返回：CC 与双寄存器组的自动切换
3. **4.3** 用软件实现传统向量中断（含任务切换）
4. **4.4** 中断控制器 icontrol：把多路中断合成一条线

### 4.1 ZipCPU 的中断模型：为什么没有向量表

#### 4.1.1 概念说明

大多数 CPU 用一张**中断向量表（interrupt vector table）**来响应中断：中断一到，硬件查表得到一个入口地址，把 PC 压栈后跳过去执行对应的中断服务程序（ISR）。不同中断有不同的入口，这就是「向量化」。

ZipCPU **不走这条路**。规范明确写道：

> The ZipCPU does not maintain any interrupt vector tables.

它靠的是 **supervisor / user 双模式 + 一条 `RTU` 指令**。思路极其朴素：

- 复位后 CPU 在 **supervisor 模式**（中断永远关闭）。
- supervisor 把一切准备好后，执行 `RTU`（return to userspace）切到 **user 模式**（中断永远打开）。
- 用户程序跑着跑着，一旦发生**中断（硬件产生）/ 陷阱 trap（软件产生）/ 异常 exception（故障）**三者之一，CPU 就**自动回到 supervisor 模式**——而且**就在「上一次离开 supervisor 的地方」继续执行**。

关键点：supervisor 的主循环里那条 `RTU` 之后紧跟的指令，就是「中断处理程序」的入口。不需要查表，也不需要专门的入口地址寄存器。

#### 4.1.2 核心流程

把整个生命周期画成状态流转：

```
        复位
         │
         ▼
   ┌─────────────┐  执行 RTU (开 GIE)
   │ supervisor  │ ──────────────────► ┌─────────────┐
   │  (中断关)   │                     │    user     │
   │  准备现场    │ ◄────────────────── │  (中断开)   │
   └─────────────┘   中断/trap/异常     │  跑用户程序  │
         ▲           (硬件自动关 GIE)   └─────────────┘
         │                                  │
         └──────── 在 RTU 之后恢复执行 ◄────┘
                   (回到 supervisor 寄存器组)
```

注意「回到 supervisor 模式」并不是跳到某个固定向量地址，而是**接着 supervisor 上次停下的那条指令往下走**——也就是上次那条 `RTU` 的下一条。这一点和绝大多数 CPU 截然不同，规范在 Operating Modes 里特别强调：

> the CPU will return to supervisor mode *at the instruction where it left off*. This also means that the ZipCPU does not support any interrupt vectors.

至于这次「回到 supervisor」到底是中断、陷阱还是异常触发的，硬件**不告诉你**，要由 supervisor 软件自己读 `uCC` 来判断（见 4.2）。

#### 4.1.3 源码精读

规范的 Interrupt Handling 章节，整段只有十几行，却定下了整个中断模型的基调：

[doc/src/spec.tex:L1537-L1549](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1537-L1549) —— 说明：没有中断向量表；中断时只是从 user 切回 supervisor；因为当初进入 user 靠的就是 `RTU`，所以中断后 supervisor 就从那条 `RTU` 之后接着跑；至于这次返回到底是中断、trap 还是异常，由 supervisor 自己判断。

这段里最关键的一句是「the supervisor just simply starts executing code immediately after that `RTU` instruction」——它定义了「中断入口」就是 supervisor 主循环里 `RTU` 的下一条指令。

同样的结论也写在 Operating Modes：

[doc/src/spec.tex:L363-L377](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L363-L377) —— 说明：两种模式与中断的绑定关系——user 模式中断恒开，supervisor 模式中断恒关；CPU 复位进 supervisor；用 `RTU` 切到 user；遇到中断或故障则回到 supervisor，停在原指令处，因此不支持中断向量。

README 用一句话向用户概括了这套设计：

[README.md:L34](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L34) —— 说明：CPU 没有中断向量，而是有两套寄存器组；任何中断发生时，CPU 只是从用户寄存器组切到监管寄存器组；这简化了中断处理，因为 CPU 自动在「打开中断」和「收到下一个中断」之间保存、保持并恢复 supervisor 的上下文；可选的 `icontrol` 外设可把多路外部中断合并成一条中断线。

#### 4.1.4 代码实践

**实践目标**：用一个真实的汇编片段验证「中断后回到 `RTU` 之后的指令」这一说法，并理解 `RTU` 之后立刻读 `uCC` 能判出原因。

**操作步骤**：

1. 打开 [bench/asm/simtest.s](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simtest.s)，定位 `traptest`（约 L266）。它的结构是：

   ```asm
   traptest:
       bra  traptest_supervisor    ; 先跳到 supervisor 段
   traptest_user:
       trap 1                      ; 用户态主动发陷阱
       busy
   traptest_supervisor:
       mov  traptest_user(pc),upc  ; 给用户设好入口 PC
       rtu                         ; 切到用户态 → 执行 traptest_user → trap 触发 → 回到这里
       mov  ucc,r0                 ; 读【用户】CC，判这次返回的原因
       tst  0x0200,r0              ; 检查 bit9（trap 位）
       tst.nz 0x020,r0             ; 检查 bit5（GIE 位）
       bz   test_failure
   ```

2. 顺着箭头跟踪一次执行：`rtu` 之后控制权交给 `traptest_user`，那里执行 `trap 1`，于是 CPU 又自动回到 `traptest_supervisor` 中 `rtu` 的下一条 `mov ucc,r0`。

**需要观察的现象**：

- `rtu` 之后**不会**顺序执行到 `busy`，而是被 `trap` 弹回到 `mov ucc,r0`。
- `tst 0x0200,r0`（bit9）应当命中，说明这次「返回」是 trap 引起的。
- `tst.nz 0x020,r0`（bit5）也应当命中——因为你读的是 **uCC**，而用户态的 GIE 位永远读出 1。

**预期结果**：两个 `tst` 都命中，程序不跳 `test_failure`。

**待本地验证**：如果你在 `sim/verilator` 下跑（`make stest` 或 `make test`，见 u1-l4），看不到 `trap` 失败的报错即为通过。本讲未替你执行该命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ZipCPU 不需要中断向量表就能找到中断处理入口？

> **答案**：因为中断/陷阱/异常发生后，CPU 回到 supervisor 模式并**在「上一次 `RTU` 之后」继续执行**。supervisor 主循环里 `RTU` 的下一条指令就是入口，位置在编译期就固定了，无需运行时查表。

**练习 2**：CPU 复位后处于哪种模式？如何进入用户模式？user 模式下中断是开还是关？

> **答案**：复位处于 supervisor 模式（中断关）。执行 `RTU` 进入 user 模式（中断开）。规范明确：user 模式中断恒开，supervisor 模式中断恒关。

---

### 4.2 中断进入与返回：CC 与双寄存器组的自动切换

#### 4.2.1 概念说明

本模块回答「中断发生的那一瞬间，硬件到底做了什么」。答案是：**几乎什么都不用做**——这正是双寄存器组最聪明的地方。

在传统 CPU 上，响应中断要先**把当前寄存器压栈**（至少压 PC 和状态字），才能放心去执行 ISR。ZipCPU 不压栈，因为它有两套物理寄存器组：

- 用户程序用 user 组（uR0–uR15、uPC、uCC）。
- supervisor 用 supervisor 组（sR0–sR15、sPC、sCC）。

切换模式 = 切换寄存器组，而切换寄存器组 = 翻转 GIE 这一位（因为 GIE 同时是寄存器地址的第 5 位）。所以「关 GIE」这一步**同时**就完成了「切到 supervisor 寄存器组」，用户的 R0–R15、PC、CC 原封不动地留在 user 组里，根本不需要存到内存。

#### 4.2.2 核心流程

一次「中断进入」硬件自动完成的动作序列：

1. **清 GIE（CC bit5）** → CPU 立刻处于 supervisor 模式（中断从此关闭，supervisor 不会被再次打断）。
2. **寄存器组自动切换** → 读写的 R0–R15 现在指向 supervisor 组；用户组的 uPC/uCC 等保持原值不动。
3. **记录原因** → 引起本次返回的事件类型被记进 **用户** 的 `uCC`：trap（bit9）、bus error（bit10）、divide-by-zero（bit11）、illegal instruction（bit8）等。注意这些状态位落在 `uCC` 而非 `sCC`，因为故障发生在用户态。
4. **在 `RTU` 之后恢复执行** → supervisor 的 sPC/sR0–sR15 都是它上次离开时的值，于是直接接着主循环往下跑。

一次「中断返回」（`RTU`）则反过来：

1. supervisor 把要恢复的用户 PC 写进 `uPC`（如需要）。
2. 执行 `RTU`，它等价于 `OR $GIE,CC`，把 bit5 置 1 → 切回 user 组。
3. trap 位在「任何返回 user 模式时」被自动清零。
4. 从 `uPC` 继续执行用户程序。

用一张表对照「进入」时哪些东西变了、哪些没变：

| 对象 | 中断进入时 | 说明 |
| --- | --- | --- |
| GIE（bit5） | 1 → 0 | 自动清零，关中断 + 切组 |
| 当前寄存器组 | user → supervisor | 翻转地址第 5 位即完成 |
| uR0–uR15 / uPC / uCC | **不变** | 原样保留在 user 组，无需压栈 |
| sR0–sR15 / sPC | **不变** | supervisor 上下文早已就位 |
| uCC 的异常位 | 被置位 | 记录 trap/故障原因，供 supervisor 读取 |

> 关键洞察：因为现场从不落盘，supervisor 在「打开中断」和「收到下一个中断」之间的上下文是**自动保存、保持、恢复**的——这正是 README 那句话的含义。

#### 4.2.3 源码精读

**GIE 位的双重身份**——它既是中断使能，又是寄存器地址的第 5 位，这是整个机制的根：

[doc/src/spec.tex:L504-L518](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L504-L518) —— 说明：bit5 是全局中断使能位；置位则中断开（user 模式），清零则中断关（supervisor 模式）；该位同时构成寄存器地址的第 5 位，控制默认读哪套寄存器组；因此从 supervisor 切到 user 只需置这一位；随后的中断或异常会自动清这一位，把 CPU 拉回 supervisor。

**异常原因记录在 `uCC`**——`CC` 的 bit 8–15 是各类异常状态位：

[doc/src/spec.tex:L440-L465](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L440-L465) —— 说明：`CC` 位定义表。注意 bit8 非法指令、bit9 陷阱（且「任何返回 user 模式时清零」）、bit10 总线错、bit11 除零、bit12（预留）浮点异常——这些就是 supervisor 读 `uCC` 后用来判因的依据。

**真实的判因代码**——`simtest.s` 的 `testbench` 段在 `RTU` 之后立刻读 `uCC` 并断言其值：

[bench/asm/simtest.s:L280-L291](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simtest.s#L280-L291) —— 说明：`testbench` 设好 `upc`/`uSP` 后 `rtu` 放用户程序跑；用户程序正常结束、发 `trap 0`（成功）回到这里；随后 `mov ucc,r0; and 0x0ffff,r0; CMP 0x220,r0` 断言用户 CC 低 16 位等于 `0x220`。

这里 `0x220 = 0x200 | 0x20`，即 bit9（trap）+ bit5（GIE）。它同时验证了两件事：① 这次返回确实由 trap 引起（bit9）；② 读的是 `uCC`，故 GIE 位读出恒为 1（bit5）。这条断言是本模块全部理论的活样本。

#### 4.2.4 代码实践

**实践目标**：亲手从一条断言里读出「中断进入」时硬件在 `uCC` 上留下的痕迹。

**操作步骤**：

1. 重新看上面引用的 [simtest.s:L280-L291](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simtest.s#L280-L291)。
2. 把期望值 `0x220` 拆成二进制位，对照 4.2.3 引用的 `CC` 位定义表，逐位说明每一位的含义。
3. 思考：如果把断言改成 `CMP 0x200,r0`（去掉 bit5），在什么前提下依然成立？在什么前提下会失败？

**需要观察的现象 / 预期结果**：

- `0x220` = bit9（trap，用户主动发的成功 trap）+ bit5（GIE，因读 uCC 恒为 1）。
- 若改成 `CMP 0x200,r0`：在「用户态确实发生过 trap」时仍成立；但因为它丢掉了「这是 uCC」这个判据，单凭它无法区分读的是 uCC 还是 sCC，作为断言会变弱。

**待本地验证**：可选地在 `sim/verilator` 中 `make stest` 跑通 `simtest`，确认该断言不会被触发为失败。本讲未替你执行。

#### 4.2.5 小练习与答案

**练习 1**：中断发生时，硬件自动完成了哪几件事？

> **答案**：① 清 GIE（关中断并切到 supervisor 寄存器组）；② 把事件原因记进 `uCC` 的对应异常位；③ 在上次 `RTU` 的下一条指令恢复 supervisor 执行。全程**不向内存压栈**。

**练习 2**：为什么 supervisor 模式下永远不会被中断？

> **答案**：supervisor 模式下 GIE 恒为 0，而规范规定中断只在 GIE=1（user 模式）时才被受理。所以 supervisor 主循环一旦开始就会跑完，直到它主动 `RTU`。

**练习 3**：`RTU` 指令等价于对 `CC` 做什么操作？它还顺带清了哪个位？

> **答案**：等价于 `OR $GIE,CC`（把 bit5 置 1，切回 user 组）。它还顺带清零 trap 位（bit9），因为规范说 trap 位「在任何返回 user 模式时清零」。

---

### 4.3 用软件实现传统向量中断（含任务切换）

#### 4.3.1 概念说明

没有硬件向量，并不意味着不能「像有向量那样」编程。规范专门给了一节 **Traditional Interrupt Handling**：用可编程中断控制器（PIC，即 `icontrol`）配合 supervisor 状态，在软件里**模拟**出传统向量中断的体验。

思路是：supervisor 跑一个 `while(true)` 主循环，每次循环开头 `RTU` 放用户程序跑；一旦中断/陷阱/异常把控制权弹回来，就按下面顺序判因：

1. 先看 `uCC` 是不是 trap 位（bit9）置位 → 用户主动请求 supervisor 服务。
2. 再看 `uCC` 是不是有 bus error / 除零 / 浮点错 → 用户程序崩了，通常打印栈并重启。
3. 以上都不是 → 说明是**纯硬件中断**，于是去查 PIC，看 15 路里哪几路同时「被使能」且「已触发」，依次调用对应的 ISR。

注意第三步：因为 CPU 只有一条中断线，**「到底是哪个外设中断的」由 PIC 告诉你**，而不是由向量地址告诉你——这就是「软件向量化」。

#### 4.3.2 核心流程

supervisor 主循环（伪代码，对照规范 Tbl. traditional-isr）：

```
while (true) {
    zip_rtu();                                   // 放用户跑，直到被打回
    if (ucc & CC_TRAPBIT)      { ... 处理用户请求 ... }
    else if (ucc & (BUSERR|FPUERR|DIVERR)) { _start(); }   // 故障 → 重启
    else {                                        // 纯中断：查 PIC
        int picv   = zip->pic;                    // 读 PIC：高16位=使能, 低16位=触发
        int active = (picv >> 16) & picv & 0x7fff; // 使能与触发的交集
        zip->pic   = (active << 16);              // 先关掉已触发的，防抖
        for (int i=0, msk=1; i<15; i++, msk<<=1)
            if ((active & msk) && isr_table[i])
                (isr_table[i])();                 // 调对应 ISR
    }
}
```

「使能与触发的交集」可以用一个简单式子表达——若用 \(E\) 表示各路使能位集合、\(P\) 表示各路已触发位集合，则真正会引发中断的活动集合为：

\[
A = E \cap P
\]

这正是上面 `(picv >> 16) & picv` 在做的事（高半字是 \(E\)，低半字是 \(P\)）。这与 4.4 里 `icontrol` 内部的 `w_any = (r_int_state & r_int_enable) != 0` 完全同构，只不过 PIC 还要细化到「哪几路」。

**任务切换（Context Switch）** 是另一回事，别和「响应中断」混为一谈。规范在 Context Switch 节列了切换任务时要做的步骤：判因 → 累计用户计账计数器 → **保存旧上下文** → 复位看门狗 → 处理中断 → 调用调度器选下一个任务。其中「保存旧上下文」才是真正把 16 个用户寄存器写进内存的地方——因为它要**换一个用户任务来跑**，旧任务的用户寄存器必须落盘，否则会被新任务覆盖。

> 区分两个概念：
> - **响应中断**：硬件切组即可，零内存开销。用户寄存器留在 user 组原处。
> - **任务切换**：软件把当前用户寄存器 `MOV` 到 supervisor 寄存器再 `SW` 进内存，腾出 user 组给下一个任务。

#### 4.3.3 源码精读

**传统向量中断的软件实现**——规范的完整伪代码：

[doc/src/spec.tex:L2069-L2118](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2069-L2118) —— 说明：Traditional Interrupt Handling 全节。先讲复位时 supervisor 要做的四件准备（建用户上下文、建 ISR 表、开主中断使能但不开任何子中断、`RTU`）；再给出主循环 `while(true){ zip_rtu(); ... }`，依次判 trap、判故障、否则查 PIC 的 `(picv>>16)&picv&0x07fff` 得到活动中断集合并逐路调 ISR。

**任务切换与保存上下文**——Context Switch 节及其中的 `save_context` 示例：

[doc/src/spec.tex:L2193-L2298](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2193-L2298) —— 说明：Context Switch 节，列出任一任务切换要执行的步骤（判因、累计计数、保存上下文、复位看门狗、处理中断、调度）。注意它再次强调「重入 supervisor 是在上次离开处」，这与 4.1 的模型呼应。

[doc/src/spec.tex:L2254-L2276](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2254-L2276) —— 说明：`save_context` 汇编示例，用 `MOV uR0,R2`、`MOV uR1,R3` …… 把用户寄存器先搬到 supervisor 寄存器，再用 `SW R2,(R1)`、`SW R3,4(R1)` … 写进内存。这是「跨组搬运」的典型写法，也是任务切换开销的来源。规范指出编译器后端提供了等价的 `zip_save_context(void*)` 内建函数。

#### 4.3.4 代码实践

**实践目标**：通过精读 `save_context`，亲手区分「响应中断的零开销」与「任务切换的内存开销」。

**操作步骤**：

1. 打开上面引用的 [spec.tex:L2254-L2276](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2254-L2276)。
2. 数一数：要完整保存一个用户任务，需要多少条 `MOV uRx,Ry` 和多少条 `SW`？（提示：16 个用户寄存器，每条 `MOV`/`SW` 处理一个，示例里 4 个一批以利用访存流水。）
3. 回答：如果 supervisor **只是想响应一个中断、读一下某个外设寄存器、再返回**，它需要执行 `save_context` 吗？为什么？

**需要观察的现象 / 预期结果**：

- `save_context` 要保存全部 16 个用户寄存器（含 uSP/uCC/uPC），约 16 条 `MOV` + 16 条 `SW`。
- **响应中断本身不需要 `save_context`**——用户寄存器仍在 user 组里安好；只有要**换任务**时才必须落盘，否则下一个任务的 `RTU` 会把新值写进同一份 user 组，覆盖旧任务。

**待本地验证**：本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`save_context` 里的 `MOV uR0,R2` 是在两套寄存器组之间搬运数据。它和「响应中断」有关系吗？

> **答案**：没有直接关系。响应中断靠硬件翻转 GIE 切组，零开销。`save_context` 是**任务切换**时，supervisor 把用户上下文存进内存，好让 user 组腾给别的任务。两件事经常接连发生，但机制完全不同。

**练习 2**：在「软件向量化」模型里，CPU 如何知道是哪个外设引发的中断？

> **答案**：CPU 本身只知道「有中断」。supervisor 先读 `uCC` 排除 trap/故障，确认是纯中断后，去读 PIC 寄存器，用 `(使能位) & (触发位)` 算出活动集合，再查 ISR 表逐路调用。

**练习 3**：主循环里 `zip->pic = (active<<16)` 这一句在做什么？为什么紧接着要循环查 ISR？

> **答案**：把已触发的中断在使能半字里对应位置 1、同时 bit15 写 0，等于**关闭**这些已触发的中断（电平触发，不关会立刻重触发）。关掉之后，逐路调用 ISR 处理；处理完由 ISR 决定是否重新使能。

---

### 4.4 中断控制器 icontrol：把多路中断合成一条线

#### 4.4.1 概念说明

前面反复提到「CPU 只有一条中断线」。那 15 个外设都想中断怎么办？答案是用可选的外设 **`icontrol`**（可编程中断控制器，PIC）：它挂在 Wishbone 总线上，占一个地址，把最多 15 路外部中断**合并成一条** `o_interrupt` 送给 CPU。

`icontrol` 解决三件事：

1. **多路合一**：15 条输入线 `i_brd_ints`，只要有一条「被使能」且「已触发」，就拉高 `o_interrupt`。
2. **独立使能/禁能**：每一路可单独开关，写一次只动指定那几路，不影响其它。
3. **主开关**：一个全局使能位（master enable），关掉它则无论多少路触发都不产生中断。

`icontrol` 是「软件向量化」能成立的前提——没有它，supervisor 无从知道「到底是谁中断了」。

#### 4.4.2 核心流程

`icontrol` 的一个 32 位寄存器被切成几段（和 4.3 读写的 `zip->pic` 完全对应）：

| 位 | 含义 |
| --- | --- |
| bit 31 | 主中断使能（master enable, `r_mie`） |
| bit 30…16 | 各路独立使能位（`r_int_enable`） |
| bit 15（读） | 当前「是否有任意使能中断触发」（`w_any`） |
| bit 15（写） | 写 1 = 本次写操作要修改使能位；写 0 = 本次写只禁能 |
| bit 14…0 | 各路当前触发状态（`r_int_state`），写 1 清除 |

内部逻辑只有三步：

1. **触发态** `r_int_state`：电平触发——输入线为高就置位，**写 1 清除**。
2. **使能态** `r_int_enable`：写时若 bit15=1 则按位「或」置位（开），bit15=0 则按位「与」清零（关）。
3. **合成**：当且仅当 `r_mie && (r_int_state & r_int_enable) != 0` 时，拉高 `o_interrupt`。

用公式写就是：

\[
\text{interrupt} = \text{MIE} \wedge \bigl( \bigvee_{i} (P_i \wedge E_i) \bigr)
\]

其中 \(P_i\) 是第 \(i\) 路触发态、\(E_i\) 是其使能位、\(\text{MIE}\) 是主使能。这正是 4.3 的 \(A = E \cap P\) 再「与」上主使能、最后归约成一个布尔值。

> 重要：中断是**电平触发**的。若外设那条线还高高在上，你在 `icontrol` 里清了状态位，下一拍它又会重新触发。所以正确顺序是**先清产生中断的外设，再清 `icontrol`**。

#### 4.4.3 源码精读

**模块端口**——`icontrol` 只占一个 Wishbone 地址，输入 `i_brd_ints` 是多路中断，输出 `o_interrupt` 是合并后的单线：

[rtl/peripherals/icontrol.v:L80-L95](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L80-L95) —— 说明：`icontrol` 的端口声明。`i_brd_ints[IUSED-1:0]` 是外部中断输入（`IUSED` 默认 12，最多 15）；`o_interrupt` 是送给 CPU 的唯一中断线；Wishbone 从端口只有 cyc/stb/we/data/ack/stall，单地址、零读延迟。

**寄存器位布局**——文件头注释把 32 位字的含义讲得最清楚：

[rtl/peripherals/icontrol.v:L19-L38](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L19-L38) —— 说明：注释逐位解释：bit31 全局使能；bit16–30 各路使能（写 1 且全局使能为 1 则开，全局使能为 0 则关）；bit15「任意中断待处理」指示；bit0–14 各路触发状态，写 1 清除。

**触发态、使能态、主使能的三段 always**：

[rtl/peripherals/icontrol.v:L116-L123](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L116-L123) —— 说明：`r_int_state` 的更新——电平触发，每拍把 `i_brd_ints`「或」进来；写操作时用 `r_int_state & (~i_wb_data)` 实现「写 1 清除」。

[rtl/peripherals/icontrol.v:L131-L138](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L131-L138) —— 说明：`r_int_enable` 的更新——`enable_ints`（写且 bit15=1）时按位或置位；`disable_ints`（写且 bit15=0）时按位与清零。

[rtl/peripherals/icontrol.v:L144-L151](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L144-L151) —— 说明：`r_mie` 主使能——仅当写操作的 bit15、bit31 同时为 1 时才置 1，bit15 为 0 且 bit31 为 1 时清 0。

**合成输出**——三行就把「多路合一」做完：

[rtl/peripherals/icontrol.v:L156-L166](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L156-L166) —— 说明：`w_any = (r_int_state & r_int_enable) != 0`（有任意使能的中断触发）；`o_interrupt` 寄存一拍后等于 `r_mie && w_any`。这就是 4.4.2 那条公式的 RTL 实现。

**规范侧的 PIC 位定义与配置示例**——与 RTL 逐位对应：

[doc/src/spec.tex:L2994-L3005](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2994-L3005) —— 说明：PIC 寄存器位定义表（tbl:picbits）：bit31 主使能、bit30–16 各路使能、bit15 读为当前主状态/写 1 才让使能位改动生效、bit14–0 输入状态写 1 清除。

[doc/src/spec.tex:L3017-L3033](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3017-L3033) —— 说明：使能/禁能/清除的具体规则：要开某路就「该路使能位=1 且 bit15=1」地写；要关就「该路使能位=1 且 bit15=0」地写；触发态写 1 清除；并强调电平触发——线不撤就会重触发。

[doc/src/spec.tex:L3035-L3059](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3035-L3059) —— 说明：一个完整例子——复位时写 `0x800f800f` 同时开主使能、开中断 0–3、并清它们的状态；中断处理后若还想要更多中断就写低位清除（如 `0x00000001`），若不再等中断就写「禁能+清」（如 `0x00010001`）。

**两层使能**——别把 CPU 的 GIE 和 PIC 的主使能搞混：

[doc/src/spec.tex:L3011-L3015](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3011-L3015) —— 说明：CPU 内部有一个全局中断使能（即 GIE/进入 user 模式），它与 PIC 的主使能是**两道独立闸门**——两者都得打开，中断才能真正被 CPU 受理。这呼应 4.2 的 GIE 机制。

> 补充：规范在 L2974–L2980 还提到 ZipSystem 里实际有两个级联的 `icontrol`（主、副），主控制器收 6 路本地中断 + 外部源，其中一路来自副控制器，副控制器管计账计数器等。需要更多路时，把多个 `icontrol` 串起来即可（见 icontrol.v 注释 L46–L47）。

#### 4.4.4 代码实践

**实践目标**：把 4.4.2 的公式落到具体配置值，学会「让某一路中断真正能打断 CPU」。

**操作步骤**：

1. 读上面引用的 [icontrol.v:L156-L166](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v#L156-L166)，确认 `o_interrupt` 有效的三个前提。
2. 回答：要让中断线 **#4** 产生 CPU 中断，必须同时满足哪三个条件？
3. 仿照规范示例 [spec.tex:L3035-L3059](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3035-L3059)，写出「开主使能、开第 4 路、清第 4 路状态」的一次 32 位写值。

**需要观察的现象 / 预期结果**：

- 三个前提：① `r_mie`（主使能）=1；② `r_int_enable[4]`（第 4 路使能）=1；③ `i_brd_ints[4]`（第 4 路输入线）为高。
- 配置写值：bit31（主使能）+ bit20（第 4 路使能，bit16+4）+ bit15（让使能改动生效）+ bit4（清第 4 路状态）= `0x80108010`。这与规范 L2963–L2964 给的「开中断 4」示例值完全一致。
- 别忘了第二道闸门：CPU 端的 GIE 也得是 1（即 CPU 在 user 模式），见 [spec.tex:L3011-L3015](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3011-L3015)。

**待本地验证**：本实践为「根据源码与规范推导配置值」，无需运行硬件。

#### 4.4.5 小练习与答案

**练习 1**：ZipCPU 本身能直接处理几条中断线？`icontrol` 的作用是什么？

> **答案**：只能处理 1 条。`icontrol` 把最多 15 路外部中断合并成这一条线，并提供逐路使能、主使能与触发状态查询。

**练习 2**：写出「启用中断 4 并清其状态」的 PIC 写入值，并逐位说明。

> **答案**：`0x80108010`。bit31=1 主使能；bit20=1 第 4 路使能（bit16+4）；bit15=1 让本次使能改动生效；bit4=1 清第 4 路触发状态。

**练习 3**：ZipCPU 的中断是边沿触发还是电平触发？这带来什么后果？

> **答案**：电平触发。后果是：只要外设的中断线还高，你在 `icontrol` 清了状态位，下一拍它又会重新触发。所以必须**先清外设、再清 `icontrol`**。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个综合任务。

**任务**：完整描述一次「外部中断」从发生到处理完毕的全过程，并对比传统向量中断模型，说明 ZipCPU 方案的得失。

**第 1 步——硬件自动动作序列（对应 4.1、4.2）**。假设 CPU 正在 user 模式跑用户程序，外设通过 `icontrol` 拉高了中断线。请按时间顺序写出 CPU 自动完成的事：

1. GIE（CC bit5）被清零 → CPU 进入 supervisor 模式、中断关闭。
2. 寄存器组从 user 切到 supervisor（uR0–uR15/uPC/uCC 原样留在 user 组，**不压栈**）。
3. supervisor 在「上次 `RTU` 之后」的指令恢复执行（即主循环里 `zip_rtu()` 的下一条）。

**第 2 步——软件判因与分发（对应 4.3、4.4）**。supervisor 接手后：

4. 读 `uCC`：trap 位（bit9）/ 异常位（bit8/10/11/12）都没置 → 排除软件请求与故障，断定是纯硬件中断。
5. 读 `icontrol`（`zip->pic`），用 `(使能半字) & (触发半字)` 算出活动集合，逐路调对应 ISR。
6. ISR 内先清外设、再清 `icontrol` 对应位（电平触发，顺序不能反）。
7. 执行 `RTU`（= `OR GIE,CC`）切回 user 组，从 `uPC` 续跑用户程序。

**第 3 步——对比与得失**。请填写下表（动手前先自己想，再对照下面的参考答案）：

| 维度 | 传统向量中断 | ZipCPU 双寄存器组 |
| --- | --- | --- |
| 入口定位 | 查向量表得入口地址 | 回到 `RTU` 之后，无表 |
| 现场保存 | 硬件至少压 PC/状态字 | 零保存（切组即完成） |
| 多中断识别 | 不同中断不同向量地址 | 单线，软件查 PIC 分发 |
| supervisor 可被打断？ | 通常可（嵌套） | 不可（supervisor 中断恒关） |

**参考结论**：

- **优势**：硬件极简（无向量表、无入口寄存器、响应中断零内存开销），supervisor 上下文在两次中断之间自动保持；这也正是 README 所说的「simplifies interrupt handling」。
- **代价**：① 多中断的「是谁」要靠软件查 PIC，多几条指令；② 任何**任务切换**仍要把 16 个用户寄存器存进内存（见 4.3.3 的 `save_context`），规范在「The Not so Good」也承认这点（L4214–L4222），不过相比 32 寄存器的 RISC 仍更省；③ supervisor 不可嵌套中断，实时性受主循环长度制约。

**可选动手环节（待本地验证）**：在 `bench/asm/simtest.s` 的 `traptest`（[L266-L278](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/simtest.s#L266-L278)）里，把 `trap 1` 改成 `trap 2`，重新在 `sim/verilator` 下 `make stest`，观察是否仍通过——预期通过，因为 supervisor 只检查 trap 位是否置位，并不关心 trap 号。本讲未替你执行该命令。

## 6. 本讲小结

- ZipCPU **没有中断向量表**：中断/陷阱/异常都让 CPU 从 user 切回 supervisor，并在「上次 `RTU` 之后」继续执行——入口位置编译期就定了。
- 「切模式 = 切寄存器组 = 翻转 GIE 位」三位一体，因此**响应中断零内存开销**：用户寄存器原样留在 user 组，无需压栈。
- 中断原因记录在 **`uCC`** 的异常位（trap bit9、bus err bit10、div0 bit11、illegal bit8），supervisor 读 `uCC` 即可判因；`simtest.s` 用 `CMP 0x220` 断言验证了这一点。
- 「任务切换」是另一回事：要用 `MOV uRx` + `SW` 把用户上下文存进内存（`save_context`），开销集中在这里，与「响应中断」无关。
- 没有硬件向量照样能做传统向量中断：supervisor 主循环 `RTU` → 判因 → 查 PIC → 调 ISR，把「是谁中断」交给软件。
- 可选的 `icontrol` 把最多 15 路外部中断合成 CPU 唯一一条线，电平触发、逐路使能；CPU 的 GIE 与 PIC 的主使能是两道**串联**闸门，都要开才生效。

## 7. 下一步学习建议

- **向硬件实现下沉**：本讲只讲了「规范层面的中断模型」。下一单元（u3）会进入 `rtl/core/zipcore.v`，看 RTL 如何在写回阶段控制 GIE/SLEEP 位、如何在取指被中断时清流水线——届时可回头验证本讲的「硬件自动动作序列」。
- **看外设如何产生中断**：u4 单元的 **外设讲义**（定时器 `ziptimer`、计数器 `zipcounter`、Jiffies `zipjiffies`、看门狗 `wbwatchdog`）会讲清这些外设的中断线如何接到 `icontrol` 的 `i_brd_ints`，把本讲的「电平触发」落到实处。
- **形式化验证视角**：`icontrol.v` 自带 `FORMAL` 段（L213 起），用 8 条断言证明「触发必置位」「使能且触发必产生中断」「主使能关则无中断」等契约。学完 u5 的形式化验证讲义后，可以回看这些断言，体会「中断契约」如何被数学证明。
- **亲手跑一次**：在 `sim/verilator` 下跑通 `simtest`（u1-l4 介绍过 `make stest`/`make test`），把本讲的 `traptest`/`testbench` 当作活教材，确认 `uCC == 0x220` 那条断言真的成立。
