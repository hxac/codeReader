# 中断处理：窗口式 vs 流式两种方案

## 1. 本讲目标

本讲精读驱动中唯一的中断入口 `PsiMsDaq_HandleIrq`，并把它背后的两套 IRQ（中断）方案讲透。学完后你应该能够：

- 说清楚 **窗口式 IRQ（Window based IRQ）** 与 **流式 IRQ（Stream based IRQ）** 各自的适用条件、为何二者互斥；
- 解释 `IRQVEC` 寄存器的「读改写清除」是如何完成电平敏感中断的应答（acknowledge）的；
- 看懂窗口式派发里 `lastProcWin` 游标与 `irqCalledWin` 位图如何协作，保证**每个窗口的回调恰好被调用一次**，并**屏蔽伪中断**；
- 理解用户回调末尾必须调用 `PsiMsDaq_StrWin_MarkAsFree` 才能复位 `irqCalledWin` 位、让该窗口号能被再次投递。

本讲是 u4 单元「数据采集、窗口与中断机制」的核心一讲，承接 u3-l3（初始化与寄存器访问抽象）和 u4-l1（录制模式与窗口/环形缓冲概念）。

## 2. 前置知识

在进入源码前，先建立几个本讲反复用到的小概念。它们都很通用，不熟悉的话读完本节即可继续。

- **电平触发中断（level-sensitive interrupt）**：中断信号在「条件成立」期间一直保持有效电平（本 IP 为高有效），直到软件把条件清除才撤销。与之相对的是边沿触发。电平触发的关键是：**ISR（中断服务程序）返回前必须清除中断源**，否则中断会立刻再次触发，形成死循环。头文件明确说明本 IP 的中断是 *level sensitive, high active*（[psi_ms_daq.h:47-48](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L47-L48)）。
- **写 1 清除（write-1-to-clear, W1C）寄存器**：一种寄存器约定——读它得到各比特的状态，向某一比特写 `1` 会把该比特清零（写 `0` 不影响）。`IRQVEC` 就是 W1C：读出哪些流有中断待处理，再把同样的值写回去即完成应答。
- **位图（bitmap）**：用一个整数的每一比特代表一个对象的状态。本讲里「第 `str` 位 = 第 `str` 条流」、「第 `win` 位 = 第 `win` 个窗口」。
- **回调函数（callback）**：由使用方注册、由驱动在合适时机「回过头来调用」的函数。本讲里驱动不直接处理采集数据，而是回调用户函数。
- **do-while 循环**：先执行一次循环体、再判断条件的循环结构，**保证循环体至少执行一次**。这一点对理解「伪中断屏蔽」至关重要。

本讲默认你已从 u3-l3 知道：驱动的所有寄存器读写最终都走 `PsiMsDaq_RegWrite/RegRead`，它们会叠加 `baseAddr` 后调用注入的访问函数；并从 u4-l1 知道：一条流由若干「窗口（Window）」组成，每个窗口是一次独立的录制单元，窗口号在硬件里由 `LASTWIN` 寄存器指向「最后一个完整写入内存的窗口」。

## 3. 本讲源码地图

本讲只涉及驱动层的两个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h) | 驱动公共头：类型、寄存器宏、返回码、API 声明 | IRQ 设计说明、`WinIrq_f`/`StrIrq_f` 回调类型、互斥返回码、`IRQVEC` 宏、`WinInfo_t` |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) | 驱动实现 | `PsiMsDaq_HandleIrq`、`SetIrqCallbackWin/Str`、`MarkAsFree`、流实例结构体里的中断状态字段 |

回忆 u1-l2/u3-l2：这两个文件在本仓库里是**上游 `psi_multi_stream_daq` 的拷贝**，每次打包会被上游同名文件覆盖；本仓库不修改它们，我们只读。

## 4. 核心概念与源码讲解

### 4.1 两种 IRQ 方案：设计意图、适用边界与头文件说明

#### 4.1.1 概念说明

驱动支持两套截然不同的中断处理方案，头文件开篇的 `@section irq_handling` 一节是它们的「权威说明书」：

- **窗口式 IRQ（Window based IRQ）**：驱动保证「每录完一个窗口，用户回调被**恰好调用一次**」，并自动屏蔽伪中断，还把该窗口的全部信息打包成 `PsiMsDaq_WinInfo_t` 传给回调。它**更完善、更好用**，但有前提：每个窗口都必须被软件真正处理、且在用户「确认（acknowledge）」之前不能被新数据覆盖。换成配置语言就是 `config.overwrite = false`，且回调末尾要调用 `PsiMsDaq_StrWin_MarkAsFree()` 释放窗口。
- **流式 IRQ（Stream based IRQ）**：驱动只负责「检测到是哪条流触发的中断，然后回调用户函数」，**不管这条流上新录了几个窗口、也不管窗口有没有被处理**。用户在回调里自己决定一切。它**始终可用**，但通常只在「窗口允许覆盖（overwrite=true）」这种窗口式方案失效的特殊场景才用。

一句话区分：**窗口式 = 驱动替你做窗口级派发与去重；流式 = 驱动只报「这条流有事」，剩下全靠你。**

#### 4.1.2 核心流程

两条关键约束贯穿两套方案：

1. **同一条流上，两种方案二选一，不能同时用**（见 [psi_ms_daq.h:50](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L50)）。
2. **无论哪套方案，IP 触发中断（电平、高有效）后，都由用户负责调用 `PsiMsDaq_HandleIrq()`**（见 [psi_ms_daq.h:47-48](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L47-L48)）。也就是说，驱动只提供「中断处理函数」，**接中断、注册到 GIC、把 IP 的 IRQ 信号连到这个函数」是用户的事**（这部分在 u5-l2 的 `main.c` 里示范）。

选型决策树：

```text
该流的窗口允许被覆盖吗（config.overwrite）？
├─ 否 (false) ──► 用窗口式 IRQ：SetIrqCallbackWin + 回调里 MarkAsFree
└─ 是 (true) ───► 只能用流式 IRQ：SetIrqCallbackStr（窗口式的前提不成立）
```

#### 4.1.3 源码精读

头文件对两套方案的描述集中在 [psi_ms_daq.h:38-71](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L38-L71)：

- **窗口式**小节 [psi_ms_daq.h:52-63](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L52-L63)：第 54-56 行明确「每个录到的窗口恰好回调一次、伪中断被屏蔽」；第 58-60 行点明前提是 `overwrite=false` 且用户必须用 `MarkAsFree` 确认每个窗口。
- **流式**小节 [psi_ms_daq.h:65-71](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L65-L71)：第 67-69 行说明驱动「只检测是哪条流触发」，回调「不管新录了几个窗口都会被调用」，因此「用户完全靠自己」，并建议「没有充分理由就用窗口式」。

两个回调函数类型也在头文件里定义：

- 窗口式回调类型 `PsiMsDaqn_WinIrq_f`：[psi_ms_daq.h:230-239](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L230-L239)，签名 `void cb(PsiMsDaq_WinInfo_t winInfo, void* arg)`，按值传入栈上的窗口信息。
- 流式回调类型 `PsiMsDaqn_StrIrq_f`：[psi_ms_daq.h:241-250](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L241-L250)，签名 `void cb(PsiMsDaq_StrHandle strHandle, void* arg)`，只给流句柄。

注意：`PsiMsDaq_WinInfo_t` 是**按值传递的栈上结构体**（[psi_ms_daq.h:219-228](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L219-L228)，注释提醒「not a handle, allocated on the stack」），回调返回后即失效，不可长期持有——这是 u3-l2 已建立的「值类型 vs 句柄」区分。

#### 4.1.4 代码实践

**目标**：用决策树为两个真实场景选对 IRQ 方案。

**步骤**：
1. 读 [psi_ms_daq.h:58-60](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L58-L60)，记下窗口式的两个前提。
2. 为下面两个场景各选一种方案：
   - 场景 A：监控一条慢变传感器流，每个窗口都要存档分析，绝不能丢，`overwrite=false`。
   - 场景 B：高速瞬态采集，CPU 来不及逐窗口处理，允许最新数据覆盖最旧的未处理窗口，`overwrite=true`。

**需要观察的现象 / 预期结果**：
- 场景 A 选**窗口式**（`SetIrqCallbackWin`），回调里取数据后调 `MarkAsFree`。
- 场景 B 只能选**流式**（`SetIrqCallbackStr`），因为窗口式要求 `overwrite=false`，前提不满足。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「窗口式 IRQ 不允许 `overwrite=true`」？

**参考答案**：窗口式方案承诺「每个窗口恰好回调一次」，并据此维护 `irqCalledWin` 去重。一旦允许覆盖，硬件可能在用户还没 `MarkAsFree` 时就把新数据写进旧窗口，驱动缓存的中断进度与窗口内容就会对不上，承诺无法兑现。因此头文件把 `overwrite=false` 列为窗口式方案的硬前提（[psi_ms_daq.h:58-60](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L58-L60)）。

**练习 2**：两种方案能否在同一条流上混用？

**参考答案**：不能。头文件 [psi_ms_daq.h:50](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L50) 与 [psi_ms_daq.h:374-376](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L374-L376) 都写明「Only one IRQ scheme can be used per stream」。具体的互斥校验在 4.2 节精读。

---

### 4.2 回调注册：互斥校验与回调函数类型

#### 4.2.1 概念说明

两套方案分别由两个注册函数确立：

- `PsiMsDaq_Str_SetIrqCallbackWin(strHndl, irqCb, arg)` —— 注册窗口式回调，写入流实例的 `irqFctWin` 字段；
- `PsiMsDaq_Str_SetIrqCallbackStr(strHndl, irqCb, arg)` —— 注册流式回调，写入流实例的 `irqFctStr` 字段。

二者都接受 `NULL` 作为 `irqCb` 来「注销」回调。它们最关键的工作不是赋值，而是**互斥校验**：注册任一种之前，先检查另一种是否已经被注册过；若是，则返回 `PsiMsDaq_RetCode_IrqSchemesWinAndStrAreExclusive`（值 `-11`，见 [psi_ms_daq.h:300](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L300)）。

#### 4.2.2 核心流程

```
SetIrqCallbackWin(str, cb, arg):
    若 str.irqFctStr != NULL   ──► 返回 IrqSchemesWinAndStrAreExclusive (-11)
    否则 str.irqFctWin = cb; str.irqArg = arg ──► 返回 Success

SetIrqCallbackStr(str, cb, arg):
    若 str.irqFctWin != NULL   ──► 返回 IrqSchemesWinAndStrAreExclusive (-11)
    否则 str.irqFctStr = cb; str.irqArg = arg ──► 返回 Success
```

注意它是「**单向检查**」：注册 Win 时只看 `irqFctStr` 是否非空，注册 Str 时只看 `irqFctWin` 是否非空。因为同一个 `irqArg` 字段被两种方案共用，所以必须在切换方案前先 `NULL` 掉旧回调，否则会撞互斥校验。流实例里相关字段的初值由 `PsiMsDaq_Init` 设为 `NULL`（[psi_ms_daq.c:171-172](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L171-L172)）。

#### 4.2.3 源码精读

`PsiMsDaq_Str_SetIrqCallbackWin`：[psi_ms_daq.c:336-351](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L336-L351)。互斥校验在第 [343-345](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L343-L345) 行——若 `irqFctStr` 已被设置则拒绝。

`PsiMsDaq_Str_SetIrqCallbackStr`：[psi_ms_daq.c:353-368](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L353-L368)。互斥校验在第 [360-362](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L360-L362) 行——若 `irqFctWin` 已被设置则拒绝。

这两个函数的头文件声明也各自带 `@note Only one IRQ scheme (...Win or ...Str) can be used` 提醒，见 [psi_ms_daq.h:374-376](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L374-L376) 与 [psi_ms_daq.h:392-394](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L392-L394)。完整的返回码枚举 `PsiMsDaq_RetCode_t` 在 [psi_ms_daq.h:288-301](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L288-L301)。

#### 4.2.4 代码实践

**目标**：通过阅读源码，预测一条「先 Win 后 Str」的错误调用序列的返回码。

**步骤**：
1. 读 [psi_ms_daq.c:336-368](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L336-L368)。
2. 对刚初始化、尚未注册任何回调的某条流，依次执行：
   - `PsiMsDaq_Str_SetIrqCallbackWin(str, myWinCb, NULL)` —— 预期返回什么？
   - 紧接 `PsiMsDaq_Str_SetIrqCallbackStr(str, myStrCb, NULL)` —— 预期返回什么？
3. 若想让第二步成功，第一步该改成什么？

**预期结果**：
- 第一步返回 `PsiMsDaq_RetCode_Success`（0），此时 `irqFctWin` 非空。
- 第二步因为 `irqFctWin != NULL`，命中 [psi_ms_daq.c:360-362](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L360-L362)，返回 `PsiMsDaq_RetCode_IrqSchemesWinAndStrAreExclusive`（-11）。
- 要切换方案，须先把第一步改成 `PsiMsDaq_Str_SetIrqCallbackWin(str, NULL, NULL)` 注销掉 Win 回调，使 `irqFctWin` 回到 `NULL`，再注册 Str 回调。

> 说明：以上为对源码逻辑的静态推演，未在硬件上运行；行为以源码为准。

#### 4.2.5 小练习与答案

**练习 1**：互斥校验为什么是「注册 Win 时查 `irqFctStr`、注册 Str 时查 `irqFctWin`」，而不是查自己要写的那个字段？

**参考答案**：因为要防止的是「两种方案并存」。注册 Win 时，自己要写的 `irqFctWin` 无论原值如何都会被覆盖，查它没意义；真正能说明「对方方案已启用」的是 `irqFctStr` 是否非空。反过来同理。所以「查对方字段」才是正确的互斥判据。

**练习 2**：`irqArg` 字段被两套方案共用，会带来什么后果？

**参考答案**：两套方案不能同时用，但切换方案时若不先把旧回调置 `NULL`，`irqArg` 会被后一次注册覆盖，且会撞互斥校验失败。所以切换方案的正确姿势是「先传 `NULL` 注销旧回调 → 再注册新回调」。

---

### 4.3 PsiMsDaq_HandleIrq：IRQVEC 应答与流式派发

#### 4.3.1 概念说明

`PsiMsDaq_HandleIrq(ipHandle)` 是整个驱动**唯一的中断处理入口**。用户在系统 ISR（如 Zynq 的 GIC 中断服务程序）里调用它，传入 IP 句柄。它做三件事：

1. **查中断源并应答**：读 `IRQVEC` 得到「哪些流有待处理中断」的位图，再把它原样写回（W1C）完成应答；
2. **逐流派发**：遍历所有流，对有中断的流分别处理；
3. **按方案分派**：流式方案直接调一次 `irqFctStr`；窗口式方案进入专门的窗口派发循环（4.4 节）。

`IRQVEC`（地址 `0x010`，宏见 [psi_ms_daq.h:151](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L151)）是典型的「位 = 流」编码：第 `n` 位为 1 表示第 `n` 条流有中断待处理。配合「写 1 清除」的 W1C 语义，读改写就完成了电平中断的应答。

#### 4.3.2 核心流程

```
HandleIrq(ip):
    strWithIrq = RegRead(IRQVEC)            # 读：哪些流有中断
    RegWrite(IRQVEC, strWithIrq)            # 写回同样的值：W1C 应答
    for str in 0..maxStreams-1:
        if strWithIrq 的第 str 位 == 0: continue   # 该流无中断
        if str.irqFctStr != NULL:                 # 流式方案
            str.irqFctStr(strHandle, str.irqArg)  #   只回调一次，交给用户
        if str.irqFctWin != NULL:                 # 窗口式方案（见 4.4）
            <窗口派发循环>
```

关键点：

- **先读后写回**是 W1C 寄存器的标准应答模式：读出状态、再用同值清掉这些位。若只读不清，电平中断会一直挂着，ISR 一返回就立刻再触发。
- **流式与窗口式互斥**（4.2 节保证），所以同一条流上 `irqFctStr` 与 `irqFctWin` 至多一个非空，两个 `if` 实际上只会进一个。
- 流式分支极其简单：**有中断就回调一次**，至于「这次中断期间新录了几个窗口」驱动根本不管——这正是流式方案「用户完全靠自己」的体现。

#### 4.3.3 源码精读

`PsiMsDaq_HandleIrq` 全函数：[psi_ms_daq.c:197-258](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L197-L258)。

- 读 `IRQVEC` 得到中断位图：[psi_ms_daq.c:204](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L204)。
- 写回 `IRQVEC` 完成 W1C 应答：[psi_ms_daq.c:205](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L205)。
- 遍历所有流：[psi_ms_daq.c:208](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L208)。
- 该流无中断则跳过（位测试）：[psi_ms_daq.c:214-216](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L214-L216)。
- **流式分支**：`irqFctStr` 非空则调用一次：[psi_ms_daq.c:218-221](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L218-L221)。
- 窗口式分支入口：[psi_ms_daq.c:224-225](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L224-L225)（细节在 4.4 节）。

注意第 [205 行](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L205)的应答是一次性把所有 pending 流的位都清了；而窗口式循环里第 [234 行](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L234)又对单条流反复清——这是为「吸收 ISR 执行期间新完成的窗口」服务的，4.4 节会展开。

#### 4.3.4 代码实践

**目标**：用日志跟踪一次「两条流同时中断、其中一条是流式方案」的派发，验证 IRQVEC 的位图语义。

**步骤**（源码阅读 + 思想实验，不改源码）：
1. 假设 IP 有 `maxStreams=4`，流 1 注册了流式回调 `strCb1`，流 3 也注册了流式回调 `strCb3`，其余流未启用中断。
2. 假设某时刻流 1 与流 3 同时完成窗口，`IRQVEC` 读出值为 `0b1000_0010` = `0xA`（第 1 位与第 3 位置 1）。
3. 对照 [psi_ms_daq.c:204-221](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L204-L221)，逐步推演 `strWithIrq` 的值、哪些 `str` 会进入处理、`strCb1`/`strCb3` 各被调用几次。

**需要观察的现象 / 预期结果**：
- `strWithIrq = 0xA`；写回 `0xA` 清除第 1、3 位（应答）。
- `str=0`：`0xA & (1<<0)=0` → 跳过；`str=1`：`0xA & (1<<1)=2≠0` → 进入，调用 `strCb1` 一次；`str=2`：跳过；`str=3`：`0xA & (1<<3)=8≠0` → 进入，调用 `strCb3` 一次。
- 结论：流式方案下，每条有中断的流**各回调恰好一次**，与该流新录了几个窗口无关。

> 说明：这是对源码逻辑的静态推演，未在硬件运行；行为以源码为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 [205 行](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L205)要「读出什么就写回什么」，而不是写一个全 1？

**参考答案**：W1C 寄存器里写 1 清位、写 0 不影响。写回读出的值，恰好清掉「这次确认到的待处理位」，而不会误清「读之后、写之前新置起的位」。若写全 1，会把可能新到的中断位也一并清掉，造成中断丢失。

**练习 2**：流式方案下，如果一条流在一次中断期间录完了 3 个窗口，用户回调会被调几次？

**参考答案**：**1 次**。流式分支 [psi_ms_daq.c:219-221](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L219-L221) 只判断 `irqFctStr` 是否非空就调用一次，不看窗口数。要按窗口分别处理，得用窗口式方案。

---

### 4.4 窗口式派发：lastProcWin 推进、irqCalledWin 位图与伪中断屏蔽

#### 4.4.1 概念说明

这是本讲最核心、也最精巧的部分。窗口式方案要兑现「每个窗口恰好回调一次 + 屏蔽伪中断」的承诺，靠流实例里的两个软件状态字段（[psi_ms_daq.c:18-19](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L18-L19)）：

- **`lastProcWin`（`int8_t`）**：游标，记录「上一次已经派发给用户回调的窗口号」。初值 `-1`（[psi_ms_daq.c:175](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L175)），表示「一个窗口都还没派发过」。
- **`irqCalledWin`（`uint32_t`）**：位图，第 `w` 位为 1 表示「窗口 `w` 已派发给回调、但用户尚未调用 `MarkAsFree` 释放」。初值 `0`（[psi_ms_daq.c:176](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L176)）。

硬件侧的「锚点」是 `LASTWIN` 寄存器（宏 [psi_ms_daq.h:162](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L162)），由 `PsiMsDaq_Str_GetLastWrittenWin` 读出（[psi_ms_daq.c:717-728](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L717-L728)），指向「最后一个完整写入内存的窗口号」。派发循环要做的事就是：**把 `lastProcWin` 之后、直到 `LASTWIN` 之间的新窗口，依次投递给回调**。

#### 4.4.2 核心流程

窗口派发是一个 **do-while** 循环（[psi_ms_daq.c:231-253](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L231-L253)），每轮处理一个窗口：

```text
win = lastProcWin                              # 从上次派发处起步
do:
    RegWrite(IRQVEC, 1<<str)                   # 清该流的中断位（吸收 ISR 期间新到的窗口）
    lastWin = GetLastWrittenWin(str)           # 重新读硬件锚点
    win = (win + 1) mod windows                # 前进到下一个窗口（窗口号在 [0,windows) 环绕）
    if irqCalledWin 的第 win 位 == 1:           # 该窗口已派发但用户还没释放
        break                                  #   停下，不再继续投递
    irqCalledWin 的第 win 位 = 1               # 标记「已派发，待释放」
    构造栈上 winInfo{winNr=win, ...}
    irqFctWin(winInfo, irqArg)                 # 回调用户（恰好一次）
    lastProcWin = win                          # 推进游标
while win != lastWin                           # 走到硬件锚点为止
```

四个要点：

1. **环绕前进**：窗口号在 \([0,\,\text{windows})\) 内取模递增（[psi_ms_daq.c:237](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L237)），与硬件的环形窗口缓冲一致。
2. **去重靠位图**：投递前查 `irqCalledWin` 第 `win` 位，若已置位则 `break`。这保证「同一窗口号在用户释放前不会被二次投递」——即**恰好一次**。
3. **停在第一个未释放的窗口**：因为 `overwrite=false` 时硬件也不会越过未释放窗口继续写，所以遇到未释放窗口就停是完全正确的。
4. **do-while 至少执行一次**：哪怕进入时 `lastProcWin == lastWin`（伪中断），循环体也会执行一轮——清中断位、推进 `win`、查位图。若新 `win` 的位图位已被置（典型伪中断场景），立即 `break`，回调 0 次。**这正是「屏蔽伪中断」的实现**。

「清除 IRQ 位 + 重读 lastWin」放在循环体最前面（[psi_ms_daq.c:233-235](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L233-L235)），目的是**吸收 ISR 执行期间新完成的窗口**：每轮清一次中断位，再读最新 `lastWin`，若又有新窗口，循环条件 `win != lastWin` 自然会把它们也处理掉，最后以「中断位已清、无新窗口」收尾，电平中断得以撤销。

#### 4.4.3 源码精读

窗口式派发主体：[psi_ms_daq.c:224-254](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L224-L254)。逐段：

- 进入分支前先读一次 `lastWin` 作初判：[psi_ms_daq.c:227-228](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L227-L228)。
- 游标初始化 `win = lastProcWin`：[psi_ms_daq.c:231](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L231)。
- 循环内先清该流 IRQ 位、再重读 `lastWin`：[psi_ms_daq.c:233-235](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L233-L235)。
- 环绕前进到下一窗口：[psi_ms_daq.c:236-237](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L236-L237)。
- **去重判据**：命中已置位则 `break`：[psi_ms_daq.c:238-241](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L238-L241)。
- 置位「已派发」标记：[psi_ms_daq.c:242](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L242)。
- 构造栈上 `WinInfo_t`（`winNr` = 当前窗口号）：[psi_ms_daq.c:243-247](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L243-L247)。
- 调用用户回调：[psi_ms_daq.c:248-250](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L248-L250)。
- 推进游标 `lastProcWin = win`：[psi_ms_daq.c:251-252](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L251-L252)。
- 循环条件 `while (win != lastWin)`：[psi_ms_daq.c:253](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L253)。

流实例结构体里相关字段：[psi_ms_daq.c:13-27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L13-L27)，其中 `lastProcWin`（[L18](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L18)）、`irqCalledWin`（[L19](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L19)）、`windows`（[L17](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L17)，即该流配置的窗口数 `winCnt`，由 `Str_Configure` 写入 [psi_ms_daq.c:314](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L314)）。

#### 4.4.4 代码实践

**目标**：手算「连续到来 3 个新窗口（0/1/2）但用户尚未 `MarkAsFree` 任何窗口」的完整派发过程，验证「回调次数、`irqCalledWin` 变化、伪中断屏蔽」。

**前置假设**（为使伪中断屏蔽能干净演示，取最自然配置）：某条流配置 `winCnt=3`（即 `str_p->windows=3`），窗口式 IRQ 已注册（`irqFctWin` 非空、`overwrite=false`）。初始状态由 `PsiMsDaq_Init` 设定：`lastProcWin=-1`、`irqCalledWin=0`（[psi_ms_daq.c:175-176](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L175-L176)）。硬件连续写完窗口 0、1、2，`LASTWIN=2`，触发一次电平中断。

**第一次 `HandleIrq`（针对该流，进入窗口式分支）**：

| 轮次 | 进入时 `win` | 清 IRQ、重读 `lastWin` | 前进 `win=(win+1)%3` | `irqCalledWin&(1<<win)`？ | 动作 | `irqCalledWin` 之后 | `lastProcWin` 之后 | `win!=lastWin`？ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | -1 | lastWin=2 | 0 | \(0\,\&\,1=0\) 不 break | 置位、回调窗口 0 | \(0\text{b}001=1\) | 0 | \(0\neq2\) 继续 |
| 2 | 0 | lastWin=2 | 1 | \(1\,\&\,2=0\) 不 break | 置位、回调窗口 1 | \(0\text{b}011=3\) | 1 | \(1\neq2\) 继续 |
| 3 | 1 | lastWin=2 | 2 | \(3\,\&\,4=0\) 不 break | 置位、回调窗口 2 | \(0\text{b}111=7\) | 2 | \(2=2\) 退出 |

**结果**：调用 **3 次**窗口回调（窗口 0、1、2 各一次），`irqCalledWin` 依次变为 \(1\to3\to7\)，最终 \(0\text{b}111=7\)，`lastProcWin=2`。

**第二次相同 IRQ（用户仍未 `MarkAsFree`，无新窗口，`LASTWIN` 仍为 2）**：

| 轮次 | 进入时 `win` | 前进 `win=(win+1)%3` | `irqCalledWin&(1<<win)`？ | 动作 |
| --- | --- | --- | --- | --- |
| 1 | 2 | 0 | \(7\,\&\,1=1\) **命中** | **break** |

**结果**：调用 **0 次**回调，`irqCalledWin` 保持 \(0\text{b}111=7\) 不变。该伪中断被屏蔽——因为下一个待处理窗口（环绕到 0）的 `irqCalledWin` 位仍被置位（用户尚未释放），驱动立即 `break`。

**需要观察的现象**：`irqCalledWin` 在第一次从 0 增长到 7、每个窗口恰好回调一次；第二次因命中已置位而回调 0 次——这就是「每个窗口恰好一次 + 屏蔽伪中断」的实现机制。

**预期结果**：第一次 3 次回调、`irqCalledWin=7`；第二次 0 次回调、`irqCalledWin` 仍为 7。

> 说明：`winCnt=3` 是为干净演示所做的假设。一般规则是「派发循环遇到第一个 `irqCalledWin` 已置位的窗口即停」；当环形缓冲里所有窗口都处于「已派发待释放」状态（满环）时，下一次 `HandleIrq` 必然立即 `break`，伪中断被屏蔽。这是对源码逻辑的静态推演，未在硬件运行；行为以源码为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么派发循环用 do-while（先执行后判断）而不是 while（先判断后执行）？

**参考答案**：do-while 保证循环体至少执行一次，这在「伪中断」场景下很关键：即便进入时 `lastProcWin == lastWin`，循环体也会执行一轮——清除该流 IRQ 位、重读 `lastWin`、推进 `win`、查 `irqCalledWin`。若新 `win` 的位已置（满环/未释放），立即 `break`，既清了中断位（让电平中断撤销）又回调 0 次。若用 while，进入条件不成立时连中断位都不清，电平中断会一直挂着。

**练习 2**：派发过程中，用户回调里读数据耗时较长，期间硬件又写完了一个新窗口，会发生什么？

**参考答案**：因为循环体每轮都会 `RegWrite(IRQVEC, 1<<str)` 清位并 `GetLastWrittenWin` 重读 `lastWin`（[psi_ms_daq.c:233-235](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L233-L235)），新写完的窗口会让 `lastWin` 变大，循环条件 `win != lastWin` 仍成立，于是这个新窗口也会在**同一次** `HandleIrq` 里被派发——即「吸收 ISR 期间新到的窗口」，不必等下一次中断。

**练习 3**：若把 `irqCalledWin` 位图去掉、只靠 `lastProcWin` 推进，会出什么问题？

**参考答案**：会无法屏蔽伪中断，也无法在「用户尚未释放」时停住。满环时下一次 `HandleIrq` 会再次推进 `win`、再次回调同一个窗口，违反「恰好一次」；更糟的是会把回调指向尚未释放、可能正被读写的窗口。位图正是用来记录「哪些窗口已派发待释放」，是去重与停机的判据。

---

### 4.5 MarkAsFree：应答握手与 irqCalledWin 位图复位

#### 4.5.1 概念说明

窗口式方案是「投递—确认」握手：驱动投递一个窗口（置 `irqCalledWin` 位 + 回调），用户处理完后必须**确认**这个窗口，否则该窗口号会一直被位图锁住、永远不会再被投递。确认动作就是 `PsiMsDaq_StrWin_MarkAsFree(winInfo)`。它做两件事：

1. **软件侧**：清除 `irqCalledWin` 的对应位，让该窗口号重新「可投递」；
2. **硬件侧**：向该窗口的 `WINCNT` 寄存器写 0，通知硬件「这个窗口可以接收新数据了」。

头文件把这一点写进了窗口式方案的契约：用户「必须调用 `MarkAsFree` 确认每个窗口，新数据才能录进去」（[psi_ms_daq.h:59-60](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L59-L60)）。

#### 4.5.2 核心流程

```text
MarkAsFree(winInfo):
    str.irqCalledWin 的第 winInfo.winNr 位 = 0     # 软件侧解锁
    RegWrite(WINCNT(str, winNr, strAddrOffs), 0)   # 硬件侧释放
    return Success
```

它与 4.4 的派发循环构成对称操作：派发时 `irqCalledWin |= (1<<win)`（[psi_ms_daq.c:242](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L242)），`MarkAsFree` 时 `irqCalledWin &= ~(1<<winNr)`（[psi_ms_daq.c:649](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L649)）。一置一清，恰好实现「投递—确认」的往返。

#### 4.5.3 源码精读

`PsiMsDaq_StrWin_MarkAsFree`：[psi_ms_daq.c:641-653](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L641-L653)。

- 取流号、IP 实例、流实例指针：[psi_ms_daq.c:644-647](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L644-L647)。
- **软件侧**清 `irqCalledWin` 对应位：[psi_ms_daq.c:649](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L649)。
- **硬件侧**向 `WINCNT` 写 0：[psi_ms_daq.c:650](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L650)（`WINCNT` 寄存器宏见 [psi_ms_daq.h:176](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L176)，回顾 u3-l1：其低 31 位是采样数、最高位 `ISTRIG` 标记是否含触发，写 0 即清空该窗口的录制元数据）。

头文件对 `MarkAsFree` 的声明与「必须调用」说明：[psi_ms_daq.h:558-564](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L558-L564)。

#### 4.5.4 代码实践

**目标**：在 4.4 的场景上接续，验证一次 `MarkAsFree(窗口0)` 如何解锁后续投递。

**步骤**：接 4.4 第一次 `HandleIrq` 后的状态（`irqCalledWin=0b111=7`、`lastProcWin=2`、`LASTWIN=2`、用户尚未释放任何窗口）。

1. 用户对窗口 0 调用 `PsiMsDaq_StrWin_MarkAsFree(winInfo0)`，推演 `irqCalledWin` 与窗口 0 的 `WINCNT` 寄存器变化。
2. 假设此后硬件又写完窗口 0（`LASTWIN` 从 2 环绕回 0），再次 `HandleIrq`，推演回调情况。

**需要观察的现象 / 预期结果**：

- 调 `MarkAsFree(窗口0)` 后：`irqCalledWin &= ~(1<<0)` → \(7\,\&\,\sim1 = 0\text{b}110=6\)；窗口 0 的 `WINCNT` 寄存器被写 0（硬件得知窗口 0 可复用）。`lastProcWin` 仍为 2。
- 硬件写完窗口 0（`LASTWIN=0`）后再次 `HandleIrq`：进入循环 `win=lastProcWin=2`；前进 `win=(2+1)%3=0`；查 `irqCalledWin&(1<<0)=6&1=0` 不 break → 置位（`irqCalledWin=0b111=7`）、回调窗口 0 一次、`lastProcWin=0`；循环条件 `win != lastWin` → \(0=0\) 退出。即回调 **1 次**（窗口 0），因为窗口 0 已被释放、可重新投递。

> 说明：这是对源码逻辑的静态推演，未在硬件运行；行为以源码为准。

#### 4.5.5 小练习与答案

**练习 1**：如果用户回调里忘了调用 `MarkAsFree`，长期运行会发生什么？

**参考答案**：每个被投递的窗口其 `irqCalledWin` 位永远不会被清。当环形缓冲里所有窗口都被投递过一次后，`irqCalledWin` 所有位全置 1；此后每次 `HandleIrq` 推进到下一个窗口都会命中已置位而 `break`，回调再也不被调用——窗口式派发「卡死」。同时硬件侧 `WINCNT` 不被清零，窗口不被释放，新数据无处可写（`overwrite=false` 时）。所以 `MarkAsFree` 是窗口式方案的必选收尾。

**练习 2**：`MarkAsFree` 为何要同时做「清软件位图」和「清硬件 `WINCNT`」两件事，少一件行不行？

**参考答案**：少清软件位图，则该窗口号永不再投递（驱动认为还没释放）；少清硬件 `WINCNT`，则硬件不知道该窗口可复用、不会把新数据录进去（且 `GetNoOfSamples` 等读回的仍是旧采样数）。两者分别对应「驱动可见的软件状态」和「IP 可见的硬件状态」，必须同步释放，缺一不可。

---

## 5. 综合实践

把本讲四块知识串成一条完整的「中断到达→派发→回读→释放」链路。请按顺序完成：

**背景**：一条配置为 `winCnt=4`、`overwrite=false`、窗口式 IRQ 的流，初始 `lastProcWin=-1`、`irqCalledWin=0`。下表给出硬件陆续写完窗口的时序与用户释放动作，请你扮演驱动，填出每次 `HandleIrq` 的「回调窗口号」「`irqCalledWin` 变化」「`lastProcWin` 终值」。

| 事件 | `LASTWIN` 变为 | 用户动作 | 本次 `HandleIrq` 回调哪些窗口？ | `irqCalledWin`（4 位） | `lastProcWin` |
| --- | --- | --- | --- | --- | --- |
| 写完窗口 0、1 | 1 | 无 | ？ | ？ | ？ |
| 写完窗口 2 | 2 | 释放窗口 0 | ？ | ？ | ？ |
| 伪中断（无新窗口） | 2 | 无 | ？ | ？ | ？ |
| 写完窗口 3 | 3 | 释放窗口 1、2 | ？ | ？ | ？ |

**要求**：
1. 严格按 [psi_ms_daq.c:231-253](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L231-L253) 的 do-while 规则推演，注意 `MarkAsFree` 对 `irqCalledWin` 的影响要先于对应 `HandleIrq` 生效。
2. 解释「伪中断」那一行为何回调 0 次。
3. 思考：若把 `overwrite` 改成 `true` 并改用 `SetIrqCallbackStr`，上表里「回调哪些窗口」这一列还能填吗？为什么？

**参考答案要点**（请先自己推演再对照）：
- 第 1 行：从 `lastProcWin=-1` 出发，前进到 0、1，都不命中位图，回调窗口 0、1；`irqCalledWin=0b0011`；`lastProcWin=1`。
- 第 2 行：先释放窗口 0 → `irqCalledWin=0b0010`；从 `lastProcWin=1` 前进到 2（不命中），回调窗口 2；`irqCalledWin=0b0110`；`lastProcWin=2`。
- 第 3 行：伪中断，`win=(2+1)%4=3`，`irqCalledWin&(1<<3)=0`？——这里要看窗口 3 是否已被投递。本行窗口 3 尚未写过，按规则会前进到 3 且位图位为 0……**这暴露了一个细节**：纯粹「无新窗口」的伪中断屏蔽，依赖满环（所有窗口位图皆置）或环绕到已置位窗口。该行若窗口 3 未被投递过，循环会试图派发窗口 3。这说明「伪中断被干净屏蔽」的典型前提是**缓冲已满（所有窗口均待释放）**；在未满时，`LASTWIN` 未推进到的空窗口不会被硬件置中断位，因此实际不会进入该分支。请结合「中断只在窗口真正写完时才由硬件置位」这一上游事实理解。
- 第 4 行：释放窗口 1、2 → `irqCalledWin=0b0000`；从 `lastProcWin=2` 前进到 3，回调窗口 3；`irqCalledWin=0b1000`；`lastProcWin=3`。
- 第 3 问：不能。流式方案每次中断只回调一次、不区分窗口，所以「回调哪些窗口」这列失去意义；用户得自己在回调里查 `LASTWIN` 决定处理哪些窗口。

> 说明：综合实践为基于源码逻辑的纸面推演，未在硬件运行；涉及「硬件何时置中断位」的部分依赖上游 IP-Core 行为，标注处请结合 u4-l1 与上游文档理解。

## 6. 本讲小结

- 驱动提供**两套互斥**的 IRQ 方案：窗口式（`SetIrqCallbackWin`，完善、按窗口派发，要求 `overwrite=false`）与流式（`SetIrqCallbackStr`，仅报「哪条流有事」，始终可用但用户全自理）。
- `PsiMsDaq_HandleIrq` 是唯一中断入口：先读 `IRQVEC` 得到中断位图、再原样写回完成 **W1C 应答**，再逐流按注册的方案分派。
- 流式分支极简——有中断就回调一次，与窗口数无关。
- 窗口式分支用 **do-while 循环 + `lastProcWin` 游标 + `irqCalledWin` 位图**：从上次派发处环绕前进到硬件 `LASTWIN`，遇已置位窗口即停，保证**每个窗口恰好回调一次**。
- 伪中断屏蔽来自「do-while 至少执行一轮 + 位图去重 + 满环绕回已置位窗口」；循环每轮清 IRQ 位并重读 `LASTWIN`，以吸收 ISR 期间新到的窗口。
- `PsiMsDaq_StrWin_MarkAsFree` 是「投递—确认」握手的确认端：清 `irqCalledWin` 对应位（软件）+ 向 `WINCNT` 写 0（硬件），二者缺一不可。

## 7. 下一步学习建议

- **u4-l3 窗口数据回读与去环绕回拷贝**：本讲的回调里收到 `WinInfo_t` 后，下一步就是 `PsiMsDaq_StrWin_GetDataUnwrapped` 把环形缓冲里的数据展开成线性顺序拷出来——那是对 `WINCNT/LAST/TSLO/TSHI` 寄存器的进一步运用。
- **u4-l4 寄存器字段助手、SAFE_CALL 与返回码**：本讲反复出现的 `SAFE_CALL`、`RegSetBit/RegGetField`、各类 `RetCode` 在那一讲统一讲清。
- **u5-l2 参考设计端到端主程序**：把本讲的 `HandleIrq` 放到真实 Zynq GIC 电平中断里看一遍（`main.c` 的 `Str0Irq/Str1Irq` 回调 + `Xil_DCacheInvalidateRange` + `MarkAsFree`），你会看到本讲机制在生产环境里的完整用法。
- 继续阅读建议：直接对照 [psi_ms_daq.c:197-258](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L197-L258) 与 [psi_ms_daq.h:38-71](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L38-L71)，用人话注释把 do-while 每一行翻译一遍，是检验是否真懂的最佳练习。
