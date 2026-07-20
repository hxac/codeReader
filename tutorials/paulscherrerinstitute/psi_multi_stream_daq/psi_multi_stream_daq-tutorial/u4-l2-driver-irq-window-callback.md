# 驱动中断处理与窗口回调

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `PsiMsDaq_HandleIrq()` 从读取 `IRQVEC`、应答（W1C 清零）到按流分发的整体流程；
- 区分「窗口方案（Window based IRQ）」与「流方案（Stream based IRQ）」两种互斥的回调注册方式，并知道各自适用场景；
- 沿着 `lastProcWin` 游标与 `do { ... } while (win != lastWin)` 循环，手工跟踪一次中断里多个窗口被连续回调的过程；
- 解释 `irqCalledWin` 位图如何防止「同一窗口被重复回调」，以及 `lastProcWin` + 每轮重读 `LASTWIN` 如何防止「窗口被漏掉」；
- 说明用户回调结束后调用 `PsiMsDaq_StrWin_MarkAsFree()` 做的两件事：清位图 + 写 `WINCNT=0` 把窗口还给硬件。

本讲承接 [u4-l1 中断生成机制与 IRQ FIFO](u4-l1-irq-generation-fifo.md)：硬件侧在 `psi_ms_daq_daq_sm` 里生成 `StrIrq` 脉冲、在 `reg_axi` 里聚合成 `IRQVEC`；本讲转到 CPU 侧，看 C 驱动如何消费这根中断线。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：中断是「电平触发、高有效」的一根线。** 头文件明确写到「the IP core asserts its interrupt (level sensitive, high active)」([driver/psi_ms_daq.h:47-48](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L47-L48))。电平触发意味着：只要 `IRQVEC` 里还有任意一位置位（且被 `IRQENA`、`GCFG.IrqEna` 门控放行），这根线就保持高；CPU 必须先「应答」清掉 `IRQVEC` 的位，线才会真正拉低，否则中断会反复重入。这一点直接决定了驱动里「读 `IRQVEC` → 写回原值清零」的写法。

**直觉二：驱动不是线程安全的。** 头文件 `@section thread_safety` 说明 API 若在多线程/中断与主循环间共用，必须由用户自行加保护（关中断、RTOS 互斥锁等）([driver/psi_ms_daq.h:25-36](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L25-L36))。典型用法是「先在主线程配置好、开中断后只在 ISR 里读数据」，此时无需额外保护。本讲讨论的 `HandleIrq` 与 `MarkAsFree` 默认就跑在 ISR/中断上下文里。

**直觉三：硬件用 `WINCNT` 表达「窗口是否空闲」。** 这是驱动与硬件之间最核心的约定（见 [u1-l4](u1-l4-driver-quickstart.md) 与 [u4-l1](u4-l1-irq-generation-fifo.md)）：窗口上下文里的 `WINCNT` 字段为 0 表示「该窗口空闲、可被硬件写入」，非 0 表示「该窗口已有数据、尚未被软件释放」。当 `winOverwrite=false` 时，硬件的状态机会用 `WinProtected` 协议保护 `WINCNT!=0` 的窗口不被覆盖（见 [u4-l5](u4-l5-window-protection-overwrite.md)）。本讲里 `MarkAsFree` 写 `WINCNT=0`，正是把窗口「还给硬件」的那一下。

## 3. 本讲源码地图

本讲只涉及驱动两个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [driver/psi_ms_daq.c](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c) | 驱动实现 | `PsiMsDaq_HandleIrq`、`PsiMsDaq_StrWin_MarkAsFree`、`PsiMsDaq_Str_GetLastWrittenWin`、流实例结构体 |
| [driver/psi_ms_daq.h](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h) | 驱动接口与寄存器宏 | `@section irq_handling` 文档、`IRQVEC`/`LASTWIN`/`WINCNT` 寄存器宏、`WinInfo_t`、两种回调函数类型 |

涉及的关键寄存器宏（来自头文件）：

- `PSI_MS_DAQ_REG_IRQVEC` = `0x010`，中断向量，**写 1 清零（W1C）**（[driver/psi_ms_daq.h:151](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L151)）；
- `PSI_MS_DAQ_REG_LASTWIN(n)` = `0x20C+0x10*(n)`，每流一个，硬件回写「最近一个完整写入内存的窗口号」（[driver/psi_ms_daq.h:162](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L162)）；
- `PSI_MS_DAQ_WIN_WINCNT(n,w,so)` = `0x4000+(so)*(n)+0x10*(w)`，每「流×窗口」一个，`WINCNT=0` 即空闲（[driver/psi_ms_daq.h:176](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L176)）。

## 4. 核心概念与源码讲解

### 4.1 HandleIrq 总体：读 IRQVEC、应答、按流分发

#### 4.1.1 概念说明

`PsiMsDaq_HandleIrq()` 是驱动暴露给用户 ISR 的唯一入口。头文件示例里，系统 ISR（由 OS/裸机调用）只做一件事：把 IP 句柄转交给 `PsiMsDaq_HandleIrq()`（[driver/psi_ms_daq.h:81-89](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L81-L89)）。

它的职责分三步：

1. **查询**：读 `IRQVEC`，得知是哪几路流触发了中断（每位对应一路流）；
2. **应答**：把读到的值原样写回 `IRQVEC`，利用 W1C 语义清掉这些位，让中断电平线有机会拉低；
3. **分发**：遍历所有流，对每个置位流调用其注册的回调——若该流注册的是「流方案」回调就直接调一次；若注册的是「窗口方案」回调，则进入按窗口的循环（见 4.2）。

注意第 2 步只是「整体应答一次」，窗口方案循环内部还会**逐轮再清一次**该流的位（见 4.2.2），这是为了处理「循环执行期间硬件又写完新窗口」的竞态。

#### 4.1.2 核心流程

```
HandleIrq(ipHandle):
  strWithIrq = RegRead(IRQVEC)          # 1. 查询：哪些流有中断
  RegWrite(IRQVEC, strWithIrq)          # 2. 应答：W1C 清掉这些位
  for str in 0..maxStreams-1:           # 3. 分发
      if strWithIrq 的第 str 位 == 0:   #    该流没中断，跳过
          continue
      if irqFctStr != NULL:             #    流方案：直接回调一次
          irqFctStr(strHandle, arg)
      if irqFctWin != NULL:             #    窗口方案：进入窗口循环（4.2）
          ... do-while 循环 ...
```

由于两种方案互斥注册（见 4.1.3），对同一路流，`irqFctStr` 与 `irqFctWin` 必然一空一非空，两个 `if` 实际只会进一个。

#### 4.1.3 源码精读

函数开头读 `IRQVEC` 并整体清零（W1C）：

```c
uint32_t strWithIrq;
PsiMsDaq_RegRead(ipHandle, PSI_MS_DAQ_REG_IRQVEC, &strWithIrq);
PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_IRQVEC, strWithIrq);
```

> 读出哪些流有中断，再把原值写回完成应答。见 [driver/psi_ms_daq.c:203-205](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L203-L205)。

随后遍历每路流，先判断该流是否在 `strWithIrq` 中置位：

```c
for (int str = 0; str < inst_p->maxStreams; str++) {
    PsiMsDaq_StrInst_t* str_p = &inst_p->streams[str];
    PsiMsDaq_StrHandle strHandle = (PsiMsDaq_StrHandle) str_p;
    //Continue if stream has no IRQ pending
    if (0 == (strWithIrq & (1 << str))){
        continue;
    }
    //IRQ Handling Type: Stream
    if (NULL != str_p->irqFctStr) {
        str_p->irqFctStr(strHandle, str_p->irqArg);
    }
    //IRQ Handling Type: Window
    if (NULL != str_p->irqFctWin) { ... }
}
```

> 见 [driver/psi_ms_daq.c:208-225](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L208-L225)。流方案（`irqFctStr`）只回调一次，窗口方案（`irqFctWin`）进入 4.2 的循环。

两种方案的「互斥注册」由注册函数保证：注册窗口回调时若已注册流回调则报错，反之亦然，错误码 `PsiMsDaq_RetCode_IrqSchemesWinAndStrAreExclusive`（[driver/psi_ms_daq.c:336-368](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L336-L368)）。头文件 `@section irq_handling` 对两种方案的取舍有完整说明：窗口方案「保证每个被记录的窗口恰好回调一次、抑制伪中断」，但要求 `winOverwrite=false` 且用户必须对每个窗口确认处理；流方案则把所有判断交给用户，适用于允许覆盖的特殊配置（[driver/psi_ms_daq.h:38-70](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L38-L70)）。

#### 4.1.4 代码实践

**实践目标**：确认「读 `IRQVEC` → 写回原值」确实是 W1C 应答，而不是写 0 或写全 1。

**操作步骤**：

1. 打开 [driver/psi_ms_daq.c:197-258](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L197-L258)，找到 `PsiMsDaq_HandleIrq`。
2. 对比 `PsiMsDaq_Init` 里对 `IRQVEC` 的清零写法：`PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_IRQVEC, 0xFFFFFFFF)`（[driver/psi_ms_daq.c:159](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L159)）——初始化时写全 1 是为了把上电残留的中断一次性清干净。
3. 思考：若把 `HandleIrq` 里的应答改成写 `0`，会发生什么？

**需要观察的现象 / 预期结果**：W1C 寄存器「写 0 无效、写 1 清零」。若误写成写 `0`，`IRQVEC` 的位不会被清，中断电平线一直为高，ISR 会无限重入——这正是初始化与应答都用「写 1」的原因。待本地验证：在有真实硬件时，把应答临时改成写 `0`，观察 ISR 是否疯狂重入。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HandleIrq` 在函数开头一次性清 `IRQVEC` 后，窗口方案循环内部还要**每一轮再清一次**该流的位？

**参考答案**：开头那次清的是「进入 ISR 时已置位」的所有流；但中断是电平触发的，循环执行需要若干拍，期间硬件可能又写完新窗口并把同一流的位重新置上。循环内每轮再清一次（[driver/psi_ms_daq.c:234](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L234)），既重新应答、又配合紧随其后的 `LASTWIN` 重读（[driver/psi_ms_daq.c:235](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L235)）把「循环期间到达」的新窗口也一并处理掉，避免漏窗。

**练习 2**：同一路流能不能同时注册 `irqFctStr` 和 `irqFctWin`？

**参考答案**：不能。两个注册函数互相检查对方是否已设置，违反则返回 `PsiMsDaq_RetCode_IrqSchemesWinAndStrAreExclusive`（[driver/psi_ms_daq.c:343-344](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L343-L344)、[driver/psi_ms_daq.c:360-361](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L360-L361)）。`HandleIrq` 里两个 `if` 因此实际上只会有一个分支生效。

---

### 4.2 窗口方案：lastProcWin 游标与 do-while 回调循环

#### 4.2.1 概念说明

窗口方案的核心承诺是「每个被硬件完整写入的窗口，用户回调恰好被调用一次」。要兑现这个承诺，驱动需要回答两个问题：

- **从哪个窗口开始回调？** —— 用 `lastProcWin`（流实例结构体里的 `int8_t` 字段）记录「上一次已经回调过的窗口号」，下一次从 `lastProcWin + 1` 开始。
- **回调到哪个窗口为止？** —— 读硬件寄存器 `LASTWIN`，它给出「最近一个完整写入内存的窗口号」，回调推进到 `LASTWIN` 为止。

初始化时 `lastProcWin = -1`（[driver/psi_ms_daq.c:175](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L175)），所以第一次中断时 `(lastProcWin + 1) % windows = 0`，从窗口 0 开始，正好。

`LASTWIN` 由 `PsiMsDaq_Str_GetLastWrittenWin()` 读出（[driver/psi_ms_daq.c:717-728](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L717-L728)），它读取的是硬件每流寄存器 `PSI_MS_DAQ_REG_LASTWIN(n)`。

#### 4.2.2 核心流程

循环用 `do { ... } while (win != lastWin)` 实现，每个迭代回调「下一个」窗口。窗口号在环形空间里推进，用取模保证回绕：

\[ \text{win}_{\text{next}} = (\text{win} + 1) \bmod N \]

其中 \( N \) 是该流配置的窗口数（`str_p->windows`）。整体流程：

```
win = lastProcWin                  # 游标起点
do:
    RegWrite(IRQVEC, 1<<str)       # 每轮重新应答本流中断
    lastWin = GetLastWrittenWin()  # 每轮重读，捕捉循环期间到达的新窗口
    win = (win + 1) % windows      # 推进到下一个窗口
    if irqCalledWin 的第 win 位 == 1:   # 该窗口已回调且未释放 → 停！
        break
    irqCalledWin |= (1 << win)     # 标记「已回调、未释放」
    构造 WinInfo_t{winNr=win, ...}
    irqFctWin(winInfo, arg)        # 调用用户回调
    lastProcWin = win              # 推进游标
while (win != lastWin)             # 到达 LASTWIN 则本轮结束
```

三个要点：

1. **每轮重读 `lastWin`**：循环期间硬件可能又写完新窗口，`LASTWIN` 会前移；重读保证这些新窗口在本轮就被消费，不依赖下一次中断。
2. **`while (win != lastWin)`**：当回调到 `LASTWIN` 指向的窗口时，本轮结束。配合每轮重读，能在一次 ISR 里「尽量抽干」所有已完成窗口。
3. **`break` 在更新 `lastProcWin` 之前**：一旦碰到「已回调未释放」的窗口，立刻停步，且**不更新** `lastProcWin`，下次中断从原游标继续（见 4.3）。

#### 4.2.3 源码精读

进入窗口方案分支后，先取一次 `LASTWIN` 作初值，然后以 `lastProcWin` 为游标进入 `do-while`：

```c
uint8_t lastWin;
PsiMsDaq_Str_GetLastWrittenWin(strHandle, &lastWin);

//Call user callbacks for new windows
int8_t win = str_p->lastProcWin;
do {
    //Check if new data arrived and clear stream IRQ
    PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_IRQVEC, (1 << str));
    PsiMsDaq_Str_GetLastWrittenWin(strHandle, &lastWin);
    //Choose next window
    win = (win + 1) % str_p->windows;
    //Stopp if this window was not yet marked as free by the user
    if (str_p->irqCalledWin & (1 << win)) {
        break;
    }
    str_p->irqCalledWin |= (1 << win);
    //Call user IRQ
    PsiMsDaq_WinInfo_t winInfo;
    winInfo.ipHandle = ipHandle;
    winInfo.strHandle = strHandle;
    winInfo.winNr = win;
    if (str_p->irqFctWin != NULL) {
        str_p->irqFctWin(winInfo, str_p->irqArg);
    }
    //Update State
    str_p->lastProcWin = win;
} while (win != lastWin);
```

> 见 [driver/psi_ms_daq.c:227-253](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L227-L253)。注释「Check if new data arrived and clear stream IRQ」点明了循环内清 `IRQVEC` + 重读 `LASTWIN` 的用意。

传给用户回调的 `PsiMsDaq_WinInfo_t` 是一个**栈上临时结构体**（不是句柄），包含 `winNr`、`ipHandle`、`strHandle` 三字段（[driver/psi_ms_daq.h:224-228](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L224-L228)）。头文件特别提醒它只在回调期间有效，回调返回后即失效（[driver/psi_ms_daq.h:221-222](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L221-L222)）。

#### 4.2.4 代码实践

**实践目标**：通过跟踪一次「两窗口连续完成」的中断，验证游标 `lastProcWin` 的推进。

**操作步骤**：

1. 假设某流 `windows=3`，初始 `lastProcWin=-1`、`irqCalledWin=0b000`。
2. 硬件写完窗口 0，触发中断，`LASTWIN=0`。在纸上模拟 `do-while`：
   - 第 1 轮：`win=(-1+1)%3=0`，置 `irqCalledWin` 第 0 位，回调窗口 0，`lastProcWin=0`；判断 `0 != 0` 为假 → 退出。
3. 接着硬件写完窗口 1，再次中断，`LASTWIN=1`：
   - 第 1 轮：`win=(0+1)%3=1`，置第 1 位，回调窗口 1，`lastProcWin=1`；`1 != 1` 为假 → 退出。
4. 现在设想：在中断 1 和中断 2 之间，硬件**同时**写完了窗口 0 和窗口 1（一次中断覆盖两个窗口），`LASTWIN=1`、`lastProcWin=-1`：
   - 第 1 轮：`win=0`，回调窗口 0，`lastProcWin=0`；`0 != 1` 为真 → 继续；
   - 第 2 轮：`win=1`，回调窗口 1，`lastProcWin=1`；`1 != 1` 为假 → 退出。

**需要观察的现象 / 预期结果**：第 4 步说明 `do-while` 能在一次中断里把「累积的多个已完成窗口」全部回调完，这正是「防漏」的来源——哪怕多个窗口挤在一次中断里，只要还没追上 `LASTWIN`，循环就不停。最终状态：`lastProcWin=1`、`irqCalledWin=0b011`。

#### 4.2.5 小练习与答案

**练习 1**：`lastProcWin` 为什么是 `int8_t` 且初值取 `-1`，而不是 `uint8_t` 初值 `0`？

**参考答案**：若初值为 `0`，第一次中断时 `(0+1)%N` 会跳过窗口 0、直接从窗口 1 开始回调，漏掉第一个窗口。取 `-1`（[driver/psi_ms_daq.c:175](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L175)）使得 `(-1+1)%N = 0`，第一次回调恰好落在窗口 0。用有符号类型是为了能表示这个「比 0 还早一格」的哨兵值。

**练习 2**：循环结束条件是 `win != lastWin`。若硬件在循环执行期间把 `LASTWIN` 往前推了两位，本轮会处理掉这两个新窗口吗？

**参考答案**：会。因为每轮迭代都重新读 `lastWin`（[driver/psi_ms_daq.c:235](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L235)），`lastWin` 跟着硬件前移，循环会一直跑到追上最新的 `LASTWIN` 为止（前提是中途没碰到未释放的窗口而 `break`）。

---

### 4.3 irqCalledWin 位图：防丢与防重

#### 4.3.1 概念说明

光有 `lastProcWin` 游标还不够。考虑一个危险场景：用户回调处理得慢，窗口 0 已回调但还没调用 `MarkAsFree` 释放；此时由于某种原因（电平中断去抖、ISR 退出后线未及时拉低、或 `LASTWIN` 已绕回）`HandleIrq` 被再次调用，`lastProcWin` 仍指向 0 之前的位置——会不会把窗口 0 **再回调一次**？

`irqCalledWin`（`uint32_t` 位图，每位对应一个窗口）就是这道屏障：回调某窗口前置位、用户 `MarkAsFree` 后清位。循环每推进到一个新窗口，先查它对应的位——若已置位，说明「这窗口已交付、用户还没还」，立即 `break`，绝不重复回调。

把两个字段合起来看，它们各自负责一种不变量：

| 字段 | 类型 | 语义 | 守护的不变量 |
| --- | --- | --- | --- |
| `lastProcWin` | `int8_t` | 「上一次回调到的窗口」 | **防漏**：下次从 `lastProcWin+1` 起，绝不跳号 |
| `irqCalledWin` | `uint32_t` 位图 | 「已回调但尚未释放」的窗口集合 | **防重**：已交付未释放的窗口不会被二次回调 |

因为窗口数 ≤ 32（`MaxWindows_c`，见 [u2-l1](u2-l1-common-package.md)），一个 `uint32_t` 恰好够装下所有窗口的位。

#### 4.3.2 核心流程

窗口方案下的状态机可以抽象成「游标 + 屏障」模型：

```
新中断到达:
  win = lastProcWin
  loop:
      win = (win + 1) % N
      if irqCalledWin[win] == 1:   # 屏障：前方窗口仍被用户占用
          break                     # 停步，lastProcWin 不动，等用户释放
      irqCalledWin[win] = 1         # 标记已交付
      回调 win
      lastProcWin = win             # 游标推进
      if win == LASTWIN: break      # 追上硬件，本轮结束
```

关键性质：`break` 发生在更新 `lastProcWin` **之前**。所以一旦被屏障挡住，`lastProcWin` 停在被占用窗口的前一格；用户 `MarkAsFree` 清掉该位后，下一次中断会从同一位置继续，自然恢复交付（见 4.4 与综合实践）。

#### 4.3.3 源码精读

两个关键字段在流实例结构体里：

```c
typedef struct {
    uint8_t nr;
    bool isConfigured;
    uint8_t widthBytes;
    uint8_t windows;
    int8_t lastProcWin;        // 防漏游标
    uint32_t irqCalledWin;     // 防重位图
    PsiMsDaqn_WinIrq_f* irqFctWin;
    PsiMsDaqn_StrIrq_f* irqFctStr;
    void* irqArg;
    ...
} PsiMsDaq_StrInst_t;
```

> 见 [driver/psi_ms_daq.c:13-27](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L13-L27)。初值在 `PsiMsDaq_Init` 里设为 `lastProcWin=-1`、`irqCalledWin=0`（[driver/psi_ms_daq.c:175-176](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L175-L176)）。

循环里屏障与置位紧挨在一起：

```c
//Choose next window
win = (win + 1) % str_p->windows;
//Stopp if this window was not yet marked as free by the user
if (str_p->irqCalledWin & (1 << win)) {
    break;
}
str_p->irqCalledWin |= (1 << win);
```

> 见 [driver/psi_ms_daq.c:237-242](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L237-L242)。注释直接点明「若该窗口尚未被用户标记为空闲就停下」。

注意：`break` 之后才有的 `str_p->lastProcWin = win;`（[driver/psi_ms_daq.c:252](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L252)）不会执行——这是「游标停在屏障前」的代码级保证。

#### 4.3.4 代码实践

**实践目标**：构造一个「窗口已被回调但未释放、中断再次到来」的场景，确认不会重复回调。

**操作步骤**：

1. 设 `windows=3`，初始 `lastProcWin=-1`、`irqCalledWin=0b000`。
2. 中断 A：硬件写完窗口 0，`LASTWIN=0`。
   - `win=0`，`irqCalledWin&(1<<0)=0` → 不 break；置位 → `irqCalledWin=0b001`；回调窗口 0；`lastProcWin=0`；`0==0` 退出。
3. 用户回调返回，但**故意不调用** `MarkAsFree`，于是 `irqCalledWin` 第 0 位仍为 1。
4. 中断 B（假设由于电平去抖或残留触发）：`LASTWIN` 仍为 0（硬件因 `winOverwrite=false` 被窗口 0 的 `WINCNT!=0` 保护、无法写入新数据，见 [u4-l5](u4-l5-window-protection-overwrite.md)）。
   - `win=lastProcWin=0`；迭代：`win=(0+1)%3=1`……等一下，这里 `win` 跳到了 1，而 `LASTWIN=0`。

**需要观察的现象 / 预期结果**：上面第 4 步暴露了一个边界——当 `LASTWIN` 没有前移、游标却已越过它时，循环会在「追不上 `lastWin`」与「屏障」之间如何收场需要仔细看。实际上更干净的防重演示见**综合实践**（4 窗口绕回场景）。本步的关键结论是：只要某窗口在 `irqCalledWin` 里置位，循环遇到它必 `break`，绝不会对该窗口二次回调。待本地验证：在真实硬件上让用户回调故意不释放窗口，观察 ISR 日志确认无重复回调。

#### 4.3.5 小练习与答案

**练习 1**：`break` 为什么必须发生在 `lastProcWin = win` 之前？若把这两行调换顺序会怎样？

**参考答案**：若先更新 `lastProcWin` 再判断，被屏障挡住时 `lastProcWin` 已经推进到那个「未释放」的窗口号上；下一次中断会从「未释放窗口 + 1」继续，等于把这个窗口永久跳过——既漏掉了它本身的恢复交付，也破坏了「游标 = 上一次成功回调」的语义。当前顺序（[driver/psi_ms_daq.c:239-252](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L239-L252)）保证游标停在屏障前，等用户释放后能自然续上。

**练习 2**：`irqCalledWin` 是 `uint32_t`。若某 IP 核配置了 `MaxWindows_c=32` 路窗口，位图够用吗？若超过 32 呢？

**参考答案**：刚好够——32 个窗口对应 32 位，编号 0..31 各占一位。但若窗口数 > 32（超出 `MaxWindows_c`，见 [u2-l1](u2-l1-common-package.md)），`1 << win` 会越界、位图装不下。这也是包里把 `MaxWindows_c` 上限设为 32 的原因之一。

---

### 4.4 MarkAsFree：清除位图、把窗口还给硬件

#### 4.4.1 概念说明

用户回调拿到 `WinInfo_t` 后，典型动作是「使缓存失效 → `GetDataUnwrapped` 拷出数据 → `MarkAsFree` 释放窗口」（见头文件示例 [driver/psi_ms_daq.h:91-100](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L91-L100)）。`PsiMsDaq_StrWin_MarkAsFree()` 做且仅做两件事：

1. **清位图**：`irqCalledWin &= ~(1 << winNr)`，撤掉该窗口的「已回调未释放」标记，让 4.3 的屏障对该窗口重新放行；
2. **写 `WINCNT=0`**：把该窗口在硬件上下文里的 `WINCNT` 寄存器清零，按驱动与硬件的约定（[u4-l1](u4-l1-irq-generation-fifo.md)）即「该窗口空闲、可被硬件再次写入」。

两件事缺一不可：只清位图不写 `WINCNT`，硬件仍认为窗口被占用、不会写入新数据；只写 `WINCNT` 不清位图，下一次中断循环会因屏障把该窗口挡住、无法重新交付。

#### 4.4.2 核心流程

```
MarkAsFree(winInfo):
    strNr = GetStrNr(winInfo.strHandle)
    str_p->irqCalledWin &= ~(1 << winInfo.winNr)        # 1. 软件侧：撤防重屏障
    RegWrite(WINCNT(strNr, winInfo.winNr, strAddrOffs), 0)  # 2. 硬件侧：宣告窗口空闲
```

完成后，该窗口在「软件位图」与「硬件 `WINCNT`」两个独立视角下都回到「空闲」，下一轮数据采集才能复用它。

#### 4.4.3 源码精读

函数实现非常短，两步对应两条语句：

```c
PsiMsDaq_RetCode_t PsiMsDaq_StrWin_MarkAsFree(	PsiMsDaq_WinInfo_t winInfo)
{
	//Setup
	uint8_t strNr;
	SAFE_CALL(PsiMsDaq_Str_GetStrNr(winInfo.strHandle, &strNr));
	PsiMsDaq_Inst_t* ip_p = (PsiMsDaq_Inst_t*) winInfo.ipHandle;
	PsiMsDaq_StrInst_t* str_p = (PsiMsDaq_StrInst_t*) winInfo.strHandle;
	//Implementation
	str_p->irqCalledWin &= ~(1 << winInfo.winNr);
	SAFE_CALL(PsiMsDaq_RegWrite(winInfo.ipHandle, PSI_MS_DAQ_WIN_WINCNT(strNr, winInfo.winNr, ip_p->strAddrOffs), 0));
	//Done
	return PsiMsDaq_RetCode_Success;
}
```

> 见 [driver/psi_ms_daq.c:641-653](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L641-L653)。`PSI_MS_DAQ_WIN_WINCNT` 宏的地址里 `so`（即 `strAddrOffs`）随最大窗口数向上取整到 2 的幂（在 `PsiMsDaq_Init` 里算出，[driver/psi_ms_daq.c:143](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L143)），其作用见 [u3-l4](u3-l4-context-memory-model.md)。

头文件对该函数的说明强调「窗口数据读出后必须调用它」，且 `GetDataUnwrapped` 本身**不会**应答处理，必须显式调 `MarkAsFree`（[driver/psi_ms_daq.h:549-564](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L549-L564)）。

#### 4.4.4 代码实践

**实践目标**：验证「只清位图」或「只写 `WINCNT`」都会破坏正常流转。

**操作步骤**：

1. 打开 [driver/psi_ms_daq.c:641-653](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L641-L653)。
2. **思想实验 A**：注释掉第 649 行（清位图）。则用户处理完窗口 0 后 `irqCalledWin` 第 0 位仍为 1。下一次硬件写完窗口 0 并中断时，循环 `win` 推进到 0 会命中屏障 `break`，窗口 0 永远不会被再次回调——尽管硬件已经写了新数据。
3. **思想实验 B**：注释掉第 650 行（写 `WINCNT=0`）。则软件位图清了，但硬件仍看到 `WINCNT!=0`、认为窗口被占用；`winOverwrite=false` 时硬件的状态机会用 `WinProtected` 一直挡着这个窗口（见 [u4-l5](u4-l5-window-protection-overwrite.md)），新数据写不进去，采集中断。

**需要观察的现象 / 预期结果**：A 导致「窗口被永久跳过」（漏数据），B 导致「硬件写不进、采集停滞」。两者都说明这两步是协同的、缺一不可。待本地验证：在真实硬件上分别复现 A、B，观察现象与上述推断是否一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MarkAsFree` 要同时改「软件位图」和「硬件 `WINCNT`」两处，而不是只改一处？

**参考答案**：因为防重屏障（`irqCalledWin`）活在驱动软件里、窗口占用状态（`WINCNT`）活在硬件上下文 RAM 里，两者是**独立的两个视角**。只改一处会让两边对「该窗口是否空闲」的判断不一致：只清软件位图，硬件仍保护着窗口、写不进新数据；只写 `WINCNT=0`，软件屏障仍挡着、回调不会再发。必须两边同步，窗口才能被正确回收复用。

**练习 2**：`MarkAsFree` 里用 `&= ~(1 << winInfo.winNr)` 清位，而不是直接赋值。为什么？

**参考答案**：因为 `irqCalledWin` 是**多窗口共用的位图**，同一时刻可能有多个窗口处于「已回调未释放」状态（例如用户处理慢、连续几个窗口都堆着）。用「读-改-写」清掉目标位，能保留其它窗口的位不变；直接赋值会误清掉其它窗口的标记，破坏它们的防重语义。

---

## 5. 综合实践

**任务**：构造一个 3 窗口流的场景，完整跟踪 `irqCalledWin` 与 `lastProcWin` 在「连续写完窗口 0、1，用户未及时处理，又一次中断到来，随后用户释放窗口 0」全过程中的变化，亲手验证「防重」与「恢复」。

**设定**：某流 `windows=3`（编号 0/1/2），`winOverwrite=false`，使用窗口方案回调。初始 `lastProcWin=-1`、`irqCalledWin=0b000`。

**追踪过程**：

| 时刻 | 事件 | `LASTWIN` | `win` 推进 | `irqCalledWin` | `lastProcWin` | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | 硬件写完窗口 0，中断 | 0 | -1 → 0 | 0b001 | 0 | 回调窗口 0；`0==0` 退出。用户**未** `MarkAsFree` |
| T2 | 硬件写完窗口 1，中断 | 1 | 0 → 1 | 0b011 | 1 | 回调窗口 1；`1==1` 退出。用户仍未释放 0 |
| T3 | 硬件写完窗口 2，中断 | 2 | 1 → 2 | 0b111 | 2 | 回调窗口 2；`2==2` 退出。用户仍未释放 0/1 |
| T4 | 中断再次到来（电平去抖/残留），`LASTWIN` 仍 2 | 2 | 2 → **0** | 0b111（不变） | 2（不变） | `win` 绕回到 0，但 `irqCalledWin` 第 0 位=1 → **`break`**！不回调窗口 0，**防重**生效。`lastProcWin` 因 `break` 不更新 |
| T5 | 用户调用 `MarkAsFree(窗口 0)` | — | — | 0b**110** | 2 | 软件清第 0 位；同时写 `WINCNT(窗口0)=0`，硬件解除对该窗口的保护 |
| T6 | 硬件写完窗口 0（第二轮），中断 | 0 | 2 → 0 | 0b111 | 0 | `win` 绕回到 0，第 0 位已清 → 不 break；置位、回调窗口 0、`lastProcWin=0`。**窗口 0 恢复交付** |

**关键观察**：

1. **防重（T4）**：窗口 0 已回调未释放时，哪怕中断再来、`win` 绕回到 0，屏障 `irqCalledWin & (1<<0)` 立刻 `break`，杜绝二次回调。且 `lastProcWin` 停在 2（`break` 在赋值前），为恢复留好接力点。
2. **恢复（T5→T6）**：用户 `MarkAsFree(窗口 0)` 清掉第 0 位并写 `WINCNT=0` 后，硬件得以写入窗口 0；下一次中断 `win` 再绕回到 0 时屏障已撤，窗口 0 被重新回调，游标推进到 0。
3. **为什么 T4 的 `break` 不会丢数据**：被挡住的是「已被回调、用户尚未处理完」的窗口——它的数据早已通过上一次回调交给用户了，挡住的是「重复交付」而非「新数据」。

**延伸思考（待本地验证）**：若把场景改成 `winOverwrite=true`，硬件在 T4 时会**无视** `WINCNT!=0` 直接覆盖窗口 0 的旧数据（见 [u4-l5](u4-l5-window-protection-overwrite.md)）。此时窗口方案的「每窗口恰好一次」承诺不再成立——这也是头文件强调窗口方案必须 `winOverwrite=false`、覆盖场景应改用流方案的原因（[driver/psi_ms_daq.h:52-70](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L52-L70)）。

## 6. 本讲小结

- `PsiMsDaq_HandleIrq()` 三步走：读 `IRQVEC` 查中断源 → 写回原值应答（W1C 清位，让电平线拉低）→ 遍历每路流按注册的回调分发（[driver/psi_ms_daq.c:197-258](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L197-L258)）。
- 两种方案互斥：流方案（`irqFctStr`）每次中断只回调一次、全交给用户；窗口方案（`irqFctWin`）保证每个窗口恰好回调一次，但要求 `winOverwrite=false`（[driver/psi_ms_daq.h:38-70](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L38-L70)）。
- 窗口方案用 `lastProcWin`（防漏游标）+ `do { } while (win != lastWin)`（每轮重读 `LASTWIN`）在一次中断里尽量抽干所有已完成窗口（[driver/psi_ms_daq.c:227-253](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L227-L253)）。
- `irqCalledWin` 位图是防重屏障：回调前置位、碰到已置位窗口立即 `break`（在更新 `lastProcWin` 之前），绝不二次回调同一窗口（[driver/psi_ms_daq.c:239-242](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L239-L242)）。
- `PsiMsDaq_StrWin_MarkAsFree()` 同步两个视角：软件侧 `irqCalledWin` 清位撤屏障、硬件侧写 `WINCNT=0` 还窗口（[driver/psi_ms_daq.c:641-653](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.c#L641-L653)）。
- 驱动整体不是线程安全的：跨线程/中断共用 API 时保护责任在用户（[driver/psi_ms_daq.h:25-36](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/driver/psi_ms_daq.h#L25-L36)）。

## 7. 下一步学习建议

- 阅读 [u4-l3 驱动数据读取与环形缓冲解包](u4-l3-driver-data-unwrap.md)：本讲只讲到「回调拿到 `WinInfo_t` 并释放窗口」，回调里真正取数据的 `PsiMsDaq_StrWin_GetDataUnwrapped()` 如何在环形窗口里做两段 `memcpy` 拼接，是自然的下一站。
- 对照 [u4-l5 窗口保护、覆盖与 NewBuffer/FirstAfterEna 协议](u4-l5-window-protection-overwrite.md)：本讲反复提到的「`winOverwrite=false` 时硬件用 `WinProtected` 保护未释放窗口」、以及 `winOverwrite=true` 时的覆盖路径，在硬件侧 `psi_ms_daq_daq_sm` 里的实现细节在那里展开。
- 回看 [u4-l1 中断生成机制与 IRQ FIFO](u4-l1-irq-generation-fifo.md)：把本讲的 `IRQVEC`/`LASTWIN` 消费端与硬件侧 `StrIrq` 脉冲、IRQ FIFO、`TfDoneCnt` 的生产端对照，串成一条完整的中断链路。
- 若要验证理解，可在仿真或硬件上实现综合实践里的 3 窗口场景，在用户回调与 `MarkAsFree` 里加日志，观察 `irqCalledWin`/`lastProcWin` 是否与表格一致。
