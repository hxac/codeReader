# 寄存器字段/比特助手、SAFE_CALL 与返回码

## 1. 本讲目标

前几讲（u3-l3、u4-l2、u4-l3）我们反复看到一个现象：驱动里几乎每一个公开函数的返回值都是 `PsiMsDaq_RetCode_t`，函数体里到处是一行行 `SAFE_CALL(...)`，而真正「读改写寄存器」的脏活则交给 `PsiMsDaq_RegSetField` / `RegSetBit` 这类助手。本讲把这些「底层工具」一次性讲透——它们是整个驱动能写得如此简洁、又能在裸机/Linux/仿真三平台复用的地基。

学完本讲你应当能：

1. 说清 `PsiMsDaq_RegSetField` / `RegGetField` / `RegSetBit` / `RegGetBit` 四个助手各自做了什么，掌握 **读改写（Read-Modify-Write, RMW）** 的标准套路与掩码（mask）计算方法。
2. 逐行解释 `SAFE_CALL` 宏如何把任意一次调用的非 Success 返回码**短路**返回给上层调用者，以及它为何只能在「返回 `RetCode_t` 的函数」里使用。
3. 理解 `CheckStrNr` / `CheckWinNr` / `CheckStrDisabled` 三个内部守卫函数各自校验什么、分别返回哪个错误码。
4. 看懂完整的 `PsiMsDaq_RetCode_t` 枚举（共 12 个码），把每一个码对应到「哪个函数、在什么非法输入/状态下」返回它。
5. 了解 `PsiMsDaq_RegWrite` / `RegRead` / `RegSetField` 这组「高级调试函数」的用途与风险，知道为何头文件反复警告「仅供调试」。

## 2. 前置知识

本讲假设你已读过 u3-l1（寄存器映射）、u3-l2（句柄与数据模型）、u3-l3（初始化与寄存器访问抽象）。为独立阅读，这里重温四个关键事实：

- **两层寄存器访问栈**（u3-l3）。最底层是默认访问函数 `PsiMsDaq_RegRead_Standard` / `RegWrite_Standard`（用 `volatile` 裸指针做 MMIO）；中间层是 `PsiMsDaq_RegRead(ipHandle, addr, ...)` / `RegWrite(...)`，它们把不透明句柄还原成 `PsiMsDaq_Inst_t*`、叠加 `baseAddr`、再经函数指针分发到底层。本讲的字段/比特助手就构建在这层 `RegRead`/`RegWrite` 之上。

- **句柄即结构体指针**（u3-l2）。`PsiMsDaq_IpHandle` 实指 `PsiMsDaq_Inst_t*`（持有 `baseAddr`/`maxStreams`/`maxWindows`/`strAddrOffs`/访问函数指针），`PsiMsDaq_StrHandle` 实指 `PsiMsDaq_StrInst_t*`（持有该流的 `nr`/`windows`/`widthBytes` 等缓存）。守卫函数靠这两个结构体里的字段来做范围校验。

- **「位 = 流」的位图编码**（u3-l1）。通用寄存器 `STRENA`（流使能）、`IRQENA`（中断使能）、`IRQVEC`（中断向量）都用同一编码：第 `n` 位对应第 `n` 条流。所以 `1 << streamNr` 就是「只选第 streamNr 条流」的掩码，本讲会反复用到。

- **SCFG 是多字段压缩寄存器**（u3-l1）。逐流上下文寄存器 `SCFG` 把 `RINGBUF`(bit0)、`OVERWRITE`(bit8)、`WINCNT[20:16]`、`WINCUR[28:24]` 多个子字段压进同一个 32 位字。本讲会看到 `Str_Configure` 如何用三次 RMW 把这些子字段**互不干扰地**分别写进去——这正是 RMW 存在的根本理由。

一个贯穿全讲的术语要先点明：**RMW（Read-Modify-Write，读改写）**。当你只想改一个 32 位寄存器里的某几位、又不能影响其它位时，标准做法是「先读出整字 → 在内存里改掉目标位 → 把整字写回去」。本讲的 `RegSetField` / `RegSetBit` 就是这个套路的封装。

## 3. 本讲源码地图

本讲只涉及两个文件，焦点是驱动里的一组底层工具函数与返回码枚举：

| 文件 | 本讲关注的内容 |
|---|---|
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h) | `PsiMsDaq_RetCode_t` 枚举、`RegWrite`/`RegRead`/`RegSetField`/`RegGetField`/`RegSetBit`/`RegGetBit` 的声明与「仅供调试」警告、相关寄存器字段比特宏 |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) | `SAFE_CALL` 宏定义、`CheckStrNr`/`CheckWinNr`/`CheckStrDisabled` 守卫函数、四个字段/比特助手的 RMW 实现、`Str_Configure` 中按顺序校验的片段 |

辅助但非本讲重点：寄存器访问间接层 `PsiMsDaq_RegRead`/`RegWrite`（u3-l3）、`PsiMsDaq_Inst_t`/`PsiMsDaq_StrInst_t` 结构体（u3-l2）。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「错误传播机制 → 守卫函数 → RMW 助手 → 返回码全集」的顺序推进。前三模块是「机制」，第四模块是把这些机制产出的所有错误码汇成一张速查表。

### 4.1 SAFE_CALL 宏：把任意调用的错误短路返回上层

#### 4.1.1 概念说明

驱动有一个贯穿全局的设计契约：**所有公开 API 都用返回值（而不是异常、全局错误变量）来报告错误**，且返回值统一是 `PsiMsDaq_RetCode_t`。于是几乎每个函数体内都会出现几十次「调用一个子函数 → 检查它有没有失败 → 失败就把同一个错误码返回给我的调用者」的三步模式。

如果每次都手写这三步，代码会被错误处理淹没。`SAFE_CALL` 宏就是把这个三步模式压成一行：**「执行调用；若返回非 Success，就立刻从当前函数把该错误码原样返回。」** 这叫「短路传播」（short-circuit propagation）——错误一旦发生，就沿调用栈逐层向上冒泡，中间函数不必写任何额外代码。

#### 4.1.2 核心流程

`SAFE_CALL(fctCall)` 展开后的伪代码是：

```
{
    r = fctCall              // 1. 执行被包裹的调用，结果存入局部变量 r
    if r != Success:         // 2. 判断是否出错
        return r             // 3. 出错则从【当前函数】立刻返回 r
}
// 只有 r == Success 才会继续往下执行本函数的后续语句
```

三个要点必须记住：

1. **`return r` 返回的是「当前函数」**，不是 `fctCall` 本身。所以 `SAFE_CALL` 只能出现在「自身返回类型也是 `PsiMsDaq_RetCode_t`」的函数里——否则 `return r` 的类型对不上。
2. **错误码原样透传**。子函数返回 `-3`，当前函数也返回 `-3`，调用者拿到的就是 `-3`，便于定位。
3. **每个 `SAFE_CALL` 自带一对 `{ }`**，局部变量 `r` 有独立块作用域。所以同一个函数里连写多个 `SAFE_CALL` 不会发生 `r` 重定义冲突。

正因为第 1 点，`PsiMsDaq_Init`（返回句柄）、`PsiMsDaq_HandleIrq`（返回 `void`）、底层 `RegRead_Standard`/`RegWrite_Standard`（返回 `void`/`uint32_t`）这些**不返回 `RetCode_t`** 的函数，体内一律不能使用 `SAFE_CALL`，只能直接调用并丢弃/使用返回值。

#### 4.1.3 源码精读

`SAFE_CALL` 的定义只有三行，却是整个驱动被引用最多的宏——[psi_ms_daq.c:L44-L46](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L44-L46)：

```c
#define SAFE_CALL(fctCall) { \
		PsiMsDaq_RetCode_t r = fctCall; \
		if (PsiMsDaq_RetCode_Success != r) {return r;}}
```

逐字解读：用 `{ ... }` 包住两句话——把 `fctCall` 的返回值存进 `PsiMsDaq_RetCode_t r`；若 `r` 不等于 `Success`(0)，就 `return r`。注意行尾的 `\` 是 C 宏续行符，把三行物理行拼成一条逻辑宏定义。

一个典型调用点是 `PsiMsDaq_Str_Configure` 里的守卫调用——[psi_ms_daq.c:L282](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L282)：

```c
SAFE_CALL(CheckStrDisabled(ipHandle, strNr));
```

它等价于手写：

```c
{
    PsiMsDaq_RetCode_t r = CheckStrDisabled(ipHandle, strNr);
    if (PsiMsDaq_RetCode_Success != r) { return r; }
}
```

即：若该流当前是使能状态，`CheckStrDisabled` 返回 `-3`，`Str_Configure` 就立刻把 `-3` 返回给它的调用者，**根本不会执行后面写寄存器的语句**。

再看「不能用 SAFE_CALL」的反例。`PsiMsDaq_Init` 的复位序列里直接调 `PsiMsDaq_RegWrite` 且**丢弃返回值**——[psi_ms_daq.c:L156-L159](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L156-L159)：

```c
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_GCFG, 0);
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_STRENA, 0);
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_IRQENA, 0);
PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_IRQVEC, 0xFFFFFFFF);
```

为什么这里不 `SAFE_CALL`？因为 `PsiMsDaq_Init` 的返回类型是 `PsiMsDaq_IpHandle`（句柄），不是 `RetCode_t`；而且此时用的是默认裸机访问函数，写寄存器不可能失败，丢弃返回值安全。同理，`PsiMsDaq_HandleIrq` 返回 `void`，它体内的 `PsiMsDaq_RegRead`/`RegWrite`（[psi_ms_daq.c:L204-L205](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L204-L205)）也不能用 `SAFE_CALL`，只能裸调。

#### 4.1.4 代码实践

**目标**：通过静态阅读，验证你对 `SAFE_CALL` 短路语义的理解，特别是「错误会跳过后续语句」这一点。

**步骤**：

1. 打开 [psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c)，定位 `PsiMsDaq_Str_GetFreeWindows`（约 L421-L445）。
2. 观察它的实现：先 `SAFE_CALL(PsiMsDaq_RegGetField(...))` 读窗口计数，再做 `if (0 == cnt) freeWin++` 累加。
3. 回答：假如循环中某一次 `PsiMsDaq_RegGetField` 返回了非 Success 码，`freeWin++` 还会执行吗？`*freeWindows_p = freeWin` 赋值还会执行吗？函数最终返回什么？

**需要观察的现象 / 预期结果**：
- 一旦 `RegGetField` 返回非 Success，`SAFE_CALL` 立刻 `return r`，**跳出整个函数**。
- 因此该轮的 `freeWin++` 不执行、循环中止、末尾的 `*freeWindows_p = freeWin` 也不执行。
- 函数返回的是 `RegGetField` 透传上来的那个错误码（在当前实现里 `RegGetField` 实际只会返回 `Success`，因为底层 `RegRead` 总返回 Success；这是「机制就绪、目前用不上」的防御式写法）。
- 本结论是**纯源码静态推导**，无需运行；如需在硬件上观测短路跳转，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SAFE_CALL` 宏体里必须用 `{ ... }` 把两句话包起来？如果去掉大括号会怎样？

**答案**：因为宏里声明了局部变量 `PsiMsDaq_RetCode_t r`。大括号给它一个独立块作用域，使得同一个函数里连写多个 `SAFE_CALL(...)` 时，每个 `r` 都在自己的作用域里，互不冲突。若去掉大括号，多个 `SAFE_CALL` 展开后会在同一函数作用域里重复声明 `r`，导致编译期「变量重定义」错误。

**练习 2**：下面三个函数，哪些**可以**在体内使用 `SAFE_CALL`，哪些**不可以**？为什么？(a) `PsiMsDaq_Str_Arm`；(b) `PsiMsDaq_Init`；(c) `PsiMsDaq_HandleIrq`。

**答案**：
- (a) **可以**。`PsiMsDaq_Str_Arm` 返回 `PsiMsDaq_RetCode_t`，体内 `return r` 类型匹配（事实上它就用了 `SAFE_CALL(PsiMsDaq_RegSetBit(...))`，见 L391）。
- (b) **不可以**。`PsiMsDaq_Init` 返回 `PsiMsDaq_IpHandle`（句柄），`return r`（一个 `RetCode_t`）类型对不上。
- (c) **不可以**。`PsiMsDaq_HandleIrq` 返回 `void`，`return r`（带一个 `RetCode_t` 值）类型对不上。

**练习 3**：`PsiMsDaq_RegRead` 自己返回 `PsiMsDaq_RetCode_t`，但 `PsiMsDaq_Init` 调用它时却没用 `SAFE_CALL`。这矛盾吗？

**答案**：不矛盾。`SAFE_CALL` 是「使用」工具，受限于调用方函数的返回类型。`Init` 返回句柄，不能用 `SAFE_CALL`，但这并不妨碍它调用 `RegRead`/`RegWrite`——只是选择**不检查**返回值（丢弃）。这在 `Init` 里是安全的，因为此时装配的是默认裸机访问函数，MMIO 写不会失败。这也体现了「返回码机制是可选检查」的特点：机制提供了检查能力，调用方根据自身情况决定是否使用。

---

### 4.2 守卫函数：CheckStrNr / CheckWinNr / CheckStrDisabled

#### 4.2.1 概念说明

「守卫函数」（guard）是软件里常见模式：在真正干活之前，先检查输入参数或当前状态是否合法；不合法就立刻返回一个明确的错误码，绝不往下执行可能出错或破坏状态的逻辑。它们是「Fail-Fast（快速失败）」原则的体现。

驱动内部有三个文件作用域（`static` 级、未进头文件）的守卫函数，分别守三道关：

1. **`CheckStrNr`**——流号有没有越界？（纯内存比较，不碰硬件）
2. **`CheckWinNr`**——窗口号有没有越界？（纯内存比较，不碰硬件）
3. **`CheckStrDisabled`**——这条流现在是不是已经停了？（要读硬件寄存器 `STRENA`）

注意它们都不在头文件 `psi_ms_daq.h` 里声明——它们是**内部**工具，仅供驱动自己通过 `SAFE_CALL` 调用，不暴露给应用层。

#### 4.2.2 核心流程

三个守卫的判断逻辑都很短，但校验依据各不相同：

| 守卫函数 | 校验对象 | 判据来源 | 越界/非法时返回 |
|---|---|---|---|
| `CheckStrNr(ip, streamNr)` | 流号 | `ip->maxStreams`（Init 时由 IP 配置决定） | `IllegalStrNr`(-1) |
| `CheckWinNr(str, winNr)` | 窗口号 | `str->windows`（该流 Str_Configure 时设的 winCnt） | `IllegalWinNr`(-5) |
| `CheckStrDisabled(ip, streamNr)` | 流是否已停 | 硬件寄存器 `STRENA` 的第 streamNr 位 | `StrNotDisabled`(-3) |

两个易混点要特别强调：

- **`CheckWinNr` 比的是「该流实际配置的窗口数」，不是「IP 的 maxWindows」**。一个流可能在 `Str_Configure` 时只配了 `winCnt=4` 个窗口，那么 `windows=4`，窗口号 0..3 合法、4 及以上返回 `-5`——即使 IP 整体支持更多窗口。
- **只有 `CheckStrDisabled` 真正读硬件**。前两个是纯软件字段比较，零副作用；`CheckStrDisabled` 要发一次 AXI 读 `STRENA`，所以它内部自己也用了一次 `SAFE_CALL(PsiMsDaq_RegRead(...))`。

#### 4.2.3 源码精读

`CheckStrNr`——把句柄还原成 `PsiMsDaq_Inst_t*`，比较流号与 `maxStreams`——[psi_ms_daq.c:L79-L87](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L79-L87)：

```c
PsiMsDaq_RetCode_t CheckStrNr(PsiMsDaq_IpHandle ipHandle, const uint8_t streamNr)
{
	PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*) ipHandle;
	if (streamNr >= inst_p->maxStreams) {
		return PsiMsDaq_RetCode_IllegalStrNr;
	}
	return PsiMsDaq_RetCode_Success;
}
```

注意是 `>=`（大于等于）：流号从 0 起算，合法范围是 `[0, maxStreams-1]`，所以等于 `maxStreams` 已算越界。

`CheckWinNr`——把流句柄还原成 `PsiMsDaq_StrInst_t*`，比较窗口号与**该流的** `windows`——[psi_ms_daq.c:L89-L97](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L89-L97)：

```c
PsiMsDaq_RetCode_t CheckWinNr(PsiMsDaq_StrHandle strHandle, const uint8_t winNr)
{
	PsiMsDaq_StrInst_t* inst_p = (PsiMsDaq_StrInst_t*) strHandle;
	if (winNr >= inst_p->windows) {
		return PsiMsDaq_RetCode_IllegalWinNr;
	}
	return PsiMsDaq_RetCode_Success;
}
```

这里的 `inst_p->windows` 是 `Str_Configure` 在 [L314](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L314) 写入的 `config_p->winCnt`，反映「这条流实际启用了几个窗口」，与 IP 全局的 `maxWindows` 是两个不同概念。

`CheckStrDisabled`——唯一读硬件的守卫：读 `STRENA`，看第 streamNr 位——[psi_ms_daq.c:L68-L77](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L68-L77)：

```c
PsiMsDaq_RetCode_t CheckStrDisabled(PsiMsDaq_IpHandle ipHandle, const uint8_t streamNr)
{
	uint32_t strEna;
	SAFE_CALL(PsiMsDaq_RegRead(ipHandle, PSI_MS_DAQ_REG_STRENA, &strEna));
	if (strEna & (1 << streamNr)) {
		return PsiMsDaq_RetCode_StrNotDisabled;
	}
	return PsiMsDaq_RetCode_Success;
}
```

逐句：先用 `SAFE_CALL` 读 `STRENA`（[PSI_MS_DAQ_REG_STRENA=0x020](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L153)）到 `strEna`；再用 `strEna & (1 << streamNr)` 取出第 streamNr 位——这正是「位 = 流」编码；该位为 1 表示流正在使能，于是返回 `StrNotDisabled`(-3)。它内部那行 `SAFE_CALL(RegRead)` 自己也演示了 4.1 的短路机制：如果读寄存器失败（当前实现下不会），错误码会直接从 `CheckStrDisabled` 透传出去。

**调用点巡礼**——守卫函数在哪被 `SAFE_CALL` 调用：
- `CheckStrNr`：在 `PsiMsDaq_GetStrHandle` 取流句柄前（[L190](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L190)）、在 `PsiMsDaq_StrWin_GetNoOfSamples` 内（[L518](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L518)）。
- `CheckWinNr`：在 `PsiMsDaq_StrWin_GetNoOfSamples` 内（[L519](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L519)），确保访问窗口元数据前窗口号合法。
- `CheckStrDisabled`：在 `PsiMsDaq_Str_Configure` 内（[L282](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L282)），强制「先停后改」。

#### 4.2.4 代码实践

**目标**：用一个具体场景体会 `CheckStrDisabled` 的「先停后改」约束，以及它如何通过 `SAFE_CALL` 把错误码冒泡到应用层。

**步骤**：

1. 设想你已 `PsiMsDaq_Init` 得到 `ip`，并 `GetStrHandle(ip, 0, &str)` 得到流 0 句柄。
2. 先调 `PsiMsDaq_Str_SetEnable(str, true)` 把流 0 使能。
3. 在**未先 SetEnable(str, false)** 的情况下，直接调 `PsiMsDaq_Str_Configure(str, &cfg)`（`cfg` 是一个合法配置结构体）。
4. 追踪返回值：`Str_Configure` 内部走到 [L282](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L282) 的 `SAFE_CALL(CheckStrDisabled(...))` 会发生什么？

**需要观察的现象 / 预期结果**：
- 流 0 在 `STRENA` 中第 0 位为 1，`CheckStrDisabled` 返回 `PsiMsDaq_RetCode_StrNotDisabled`(-3)。
- `SAFE_CALL` 把 -3 短路返回，`Str_Configure` 立刻返回 -3，**不会写任何寄存器**。
- 应用层拿到 -3，即可知道「这条流还没停，得先 `SetEnable(str, false)` 再配置」。
- 此结论由源码逻辑直接得出；若要在 ZCU102 参考设计上实测该返回码，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：某 IP 配 `maxStreams=4`。应用层调用 `PsiMsDaq_GetStrHandle(ip, 5, &str)` 会得到什么返回码？为什么？

**答案**：返回 `PsiMsDaq_RetCode_IllegalStrNr`(-1)。`GetStrHandle` 先 `SAFE_CALL(CheckStrNr(ipHandle, streamNr))`（[L190](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L190)）；`streamNr=5 >= maxStreams=4`，`CheckStrNr` 返回 -1，经 `SAFE_CALL` 透传给调用者，且 `strHndl_p` 不会被赋值。

**练习 2**：一条流在 `Str_Configure` 时只配了 `winCnt=2`，随后调 `PsiMsDaq_StrWin_GetNoOfSamples(winInfo{winNr=3}, &n)`。返回什么？

**答案**：返回 `PsiMsDaq_RetCode_IllegalWinNr`(-5)。`GetNoOfSamples` 在 [L519](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L519) `SAFE_CALL(CheckWinNr(winInfo.strHandle, winInfo.winNr))`；该流 `windows=2`，`winNr=3 >= 2`，返回 -5。注意判据是「该流配置的窗口数」，不是 IP 的 `maxWindows`。

**练习 3**：`CheckStrDisabled` 为什么要读硬件寄存器，而 `CheckStrNr` / `CheckWinNr` 不用？

**答案**：因为「流是否使能」是**运行时动态状态**，由硬件 `STRENA` 寄存器反映，软件没有同步缓存它（使能/禁用是直接 RMW 写 `STRENA` 的，见 `PsiMsDaq_Str_SetEnable` [L322-L334](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L322-L334)），所以必须现读现判。而「最大流数」「该流的窗口数」是**初始化/配置时定下来的静态几何参数**，已缓存在 `PsiMsDaq_Inst_t`/`PsiMsDaq_StrInst_t` 里，纯内存比较即可，无需访问硬件。

---

### 4.3 RMW 助手：RegSetField / RegGetField / RegSetBit / RegGetBit

#### 4.3.1 概念说明

寄存器往往是「一个 32 位字里塞了多个子字段」。比如逐流上下文寄存器 `SCFG` 就同时含 `RINGBUF`(bit0)、`OVERWRITE`(bit8)、`WINCNT[20:16]`、`WINCUR[28:24]`。要改其中某一个字段而**不动其它字段**，就不能整字直接覆盖，必须 RMW：先读回整字，只修改目标位的值，再整字写回。

驱动提供四个助手封装这套逻辑，按「字段（多 bit）/ 比特（单 bit 或掩码）」「读 / 写」两两组合：

| 助手 | 操作粒度 | 动作 | 是否 RMW（写回） |
|---|---|---|---|
| `PsiMsDaq_RegSetField` | 字段（lsb..msb 多位） | 写入字段新值 | 是，读改写 |
| `PsiMsDaq_RegGetField` | 字段（lsb..msb 多位） | 读出字段当前值 | 否，只读 |
| `PsiMsDaq_RegSetBit` | 比特掩码（mask） | 置位/清零掩码位 | 是，读改写 |
| `PsiMsDaq_RegGetBit` | 比特掩码（mask） | 测试掩码位是否非零 | 否，只读 |

它们都返回 `PsiMsDaq_RetCode_t`，所以可以（且通常）被 `SAFE_CALL` 包裹。它们也是「Advanced Functions」段的成员，头文件对它们的定性是「仅供调试，否则驱动可能工作异常」——这点在 4.3.4 会展开。

#### 4.3.2 核心流程

**掩码计算**是这套助手的数学核心。对一个 `[msb:lsb]` 的字段（含 `lsb`、`msb` 两端，共 `msb-lsb+1` 位），需要一个「该字段宽度个 1」的掩码：

\[
\text{fldMsk} = 2^{(\text{msb}+1)} - 1 \quad\text{（低位连续 msb+1 个 1）}
\]

例如字段 `[20:16]`：\(\text{fldMsk} = 2^{21}-1 = \texttt{0x1F\_FFFF}\)（21 个 1，覆盖 bit0..bit20）。

- **`RegSetField(addr, lsb, msb, value)`** 做三件事：
  1. 算 `fldMsk`，并把输入 `value` 也用它截断（`value & fldMsk`），防止用户传超宽值污染相邻字段；
  2. 把掩码、截断后的值都左移 `lsb` 位，对齐到字段在寄存器里的真实位置；
  3. RMW：读回 `reg` → `reg &= ~(fldMsk<<lsb)`（清旧字段）→ `reg |= ((value&fldMsk)<<lsb)`（写新值）→ 写回。

- **`RegGetField(addr, lsb, msb, &v)`**：读回 `reg` → `v = (reg >> lsb) & fldMsk`（右移到 bit0，再掩码到字段宽度）。只读，不写回。

- **`RegSetBit(addr, mask, value)`**：掩码 `mask` 由调用方直接给出（如 `1<<streamNr` 或 `PSI_MS_DAQ_CTX_SCFG_BIT_RINGBUF`）。RMW：读回 `reg` → `reg &= ~mask`（清掉掩码位）→ 若 `value` 为真则 `reg |= mask`（置位）→ 写回。适合置/清单个标志位。

- **`RegGetBit(addr, mask, &b)`**：读回 `reg` → `b = (0 != (reg & mask))`。只要掩码内有任何一位为 1 即返回 `true`。

#### 4.3.3 源码精读

先看两个间接层基座 `PsiMsDaq_RegWrite` / `RegRead`（u3-l3 已详述，这里只复习它们「叠加 baseAddr、经函数指针分发」的角色）——[psi_ms_daq.c:L730-L752](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L730-L752)：

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegWrite(PsiMsDaq_IpHandle ipHandle, const uint32_t addr, const uint32_t value)
{
	PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*)ipHandle;
	inst_p->regWrFct(inst_p->baseAddr+addr, value);   // 加 baseAddr，分发到注入的写函数
	return PsiMsDaq_RetCode_Success;
}

PsiMsDaq_RetCode_t PsiMsDaq_RegRead(PsiMsDaq_IpHandle ipHandle, const uint32_t addr, uint32_t* const value_p)
{
	PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*)ipHandle;
	*value_p = inst_p->regRdFct(inst_p->baseAddr+addr); // 加 baseAddr，分发到注入的读函数
	return PsiMsDaq_RetCode_Success;
}
```

注意：这两个间接层**当前总是返回 Success**——因为底层 MMIO 不会「失败」。返回 `RetCode_t` 是为了能被 `SAFE_CALL` 套用、并为将来（例如带校验的总线访问）留出钩子。

**`PsiMsDaq_RegSetField`**——RMW 的完整范本，掩码计算 + 清旧写新——[psi_ms_daq.c:L754-L771](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L754-L771)：

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegSetField(PsiMsDaq_IpHandle ipHandle, const uint32_t addr,
                                        const uint8_t lsb, const uint8_t msb, const uint32_t value)
{
	uint32_t reg;
	uint32_t msk = (1 << (msb+1))-1;        // fldMsk：低位 msb+1 个 1
	uint32_t mskSft = msk << lsb;           // 掩码左移到字段位置
	uint32_t valSft = ((value & msk) << lsb); // 值截断后左移到字段位置
	SAFE_CALL(PsiMsDaq_RegRead(ipHandle, addr, &reg));  // 读
	reg &= ~mskSft;                         // 改：清旧字段
	reg |= valSft;                          // 改：写新值
	SAFE_CALL(PsiMsDaq_RegWrite(ipHandle, addr, reg));  // 写
	return PsiMsDaq_RetCode_Success;
}
```

以写 `SCFG` 的 `WINCNT[20:16]` 字段（lsb=16, msb=20）为例，套用上面三式：

- `msk = (1<<21)-1 = 0x001F_FFFF`（bit0..bit20 共 21 个 1）；
- `mskSft = 0x001F_FFFF << 16 = 0x1FFF_F0000`（bit16..bit20）；
- 若要写入 `winCnt-1 = 3`，则 `valSft = (3 & 0x1F_FFFF) << 16 = 0x3_0000`；
- RMW 后，`SCFG` 的 bit16..bit20 变成 `3`，其余位（包括 `RINGBUF` bit0、`OVERWRITE` bit8）原样保留。

**`PsiMsDaq_RegGetField`**——只读，右移 + 掩码——[psi_ms_daq.c:L773-L786](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L773-L786)：

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegGetField(PsiMsDaq_IpHandle ipHandle, const uint32_t addr,
                                        const uint8_t lsb, const uint8_t msb, uint32_t* const value_p)
{
	uint32_t reg;
	uint32_t msk = (1 << (msb+1))-1;
	SAFE_CALL(PsiMsDaq_RegRead(ipHandle, addr, &reg));
	*value_p = (reg >> lsb) & msk;          // 右移到 bit0，再掩到字段宽度
	return PsiMsDaq_RetCode_Success;
}
```

它被 `PsiMsDaq_StrWin_GetNoOfSamples` 用来取窗口采样数（字段 `[30:0]`，见 [L521-L525](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L521-L525)），也被 `PsiMsDaq_Str_CurrentWin` 用来取 `SCFG.WINCUR[28:24]`（见 [L696-L700](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L696-L700)）。

**`PsiMsDaq_RegSetBit`**——用现成掩码做单标志位的 RMW——[psi_ms_daq.c:L790-L805](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L790-L805)：

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegSetBit(PsiMsDaq_IpHandle ipHandle, const uint32_t addr,
                                      const uint32_t mask, const bool value)
{
	uint32_t reg;
	SAFE_CALL(PsiMsDaq_RegRead(ipHandle, addr, &reg));
	reg &= ~mask;                            // 先清掉掩码位
	if (value) {
		reg |= mask;                         // value 为真则置位
	}
	SAFE_CALL(PsiMsDaq_RegWrite(ipHandle, addr, reg));
	return PsiMsDaq_RetCode_Success;
}
```

它的典型用法是 `PsiMsDaq_Str_SetEnable`：`mask = 1 << strNr`，对 `STRENA` 做 RMW（[L330-L331](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L330-L331)），置位/清零某条流的使能位而不影响其它流。

**`PsiMsDaq_RegGetBit`**——测试掩码位是否非零——[psi_ms_daq.c:L807-L818](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L807-L818)：

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegGetBit(PsiMsDaq_IpHandle ipHandle, const uint32_t addr,
                                      const uint32_t mask, bool* const value_p)
{
	uint32_t reg;
	SAFE_CALL(PsiMsDaq_RegRead(ipHandle, addr, &reg));
	*value_p = (0 != (reg & mask));
	return PsiMsDaq_RetCode_Success;
}
```

它被 `PsiMsDaq_StrWin_GetPreTrigSamples` 用来测 `WINCNT` 的 `ISTRIG` 位（`mask = 1<<31`），决定窗口是否含触发（[L538-L541](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L538-L541)）。

**RMW 的真正价值：SCFG 的三次增量写入**。`PsiMsDaq_Str_Configure` 对同一个 `SCFG` 寄存器连做了三次 RMW，分别写三个不同子字段——[psi_ms_daq.c:L292-L310](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L292-L310)：

```c
SAFE_CALL(PsiMsDaq_RegSetBit(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr),
                             PSI_MS_DAQ_CTX_SCFG_BIT_RINGBUF, config_p->winAsRingbuf));   // 写 bit0
SAFE_CALL(PsiMsDaq_RegSetBit(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr),
                             PSI_MS_DAQ_CTX_SCFG_BIT_OVERWRITE, config_p->winOverwrite)); // 写 bit8
...
SAFE_CALL(PsiMsDaq_RegSetField(ipHandle, PSI_MS_DAQ_CTX_SCFG(strNr),
                               PSI_MS_DAQ_CTX_SCFG_LSB_WINCNT, PSI_MS_DAQ_CTX_SCFG_MSB_WINCNT,
                               config_p->winCnt-1));                                     // 写 [20:16]
```

这三步之所以能共存：第一次 RMW 把 `RINGBUF` 写进 `SCFG`；第二次 RMW 读回 `SCFG`（此时 `RINGBUF` 已在），保留它再叠加 `OVERWRITE`；第三次 RMW 再读回（`RINGBUF`+`OVERWRITE` 都在），保留它们再写 `WINCNT`。若改用「整字直接写」，后一次就会冲掉前一次写入的子字段——这正是 RMW 不可替代的根本原因。

> **边界提示**：掩码公式 `msk = (1 << (msb+1)) - 1` 在本驱动用到的所有字段（最高 `msb=30`，即 `WINCNT_MSB_CNT`）下都正确。它有一个隐含边界：若某字段 `msb=31`，则 `1 << 32` 对 32 位整型是未定义行为。本驱动从不声明 `msb=31` 的字段（`ISTRIG` 用的是 `1<<31` 的「比特掩码」走 `RegGetBit`，而非字段走 `RegGetField`），所以实际安全；但若你扩展驱动新增字段，需避开 `msb=31`。

#### 4.3.4 代码实践

**目标**：手算一次 `RegSetField` 的掩码与最终写入值，验证「字段写入不影响相邻位」。

**步骤**：

1. 设 `SCFG` 当前值为 `0x0100_0000`（即 `WINCUR[28:24]` 字段当前是 `1`，其余位为 0）。
2. 调用 `PsiMsDaq_RegSetField(ip, SCFG_addr, lsb=16, msb=20, value=7)` 想把 `WINCNT` 写成 7。
3. 按 4.3.3 的公式手算：`msk`、`mskSft`、`valSft` 各是多少？RMW 后 `SCFG` 的最终值是多少？`WINCUR` 字段（bit28..24）被破坏了吗？

**需要观察的现象 / 预期结果**：
- `msk = (1<<21)-1 = 0x001F_FFFF`；
- `mskSft = 0x001F_FFFF << 16 = 0x1FFF_F0000`；
- `valSft = (7 & 0x1F_FFFF) << 16 = 0x0007_0000`；
- 读回 `reg = 0x0100_0000`；`reg &= ~mskSft` → `0x0100_0000 & 0xE000_0FFFF = 0x0100_0000`（`WINCUR` 位不在 `mskSft` 范围内，原样保留）；`reg |= valSft` → `0x0100_0000 | 0x0007_0000 = 0x0107_0000`；
- 最终 `SCFG = 0x0107_0000`：`WINCNT[20:16]=7`，`WINCUR[28:24]=1` **未被破坏**。这印证了 RMW 的核心价值。
- 本结果为**源码静态推导**；在硬件上观测该寄存器值需**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`RegSetField` 里 `valSft = ((value & msk) << lsb)` 中的 `& msk` 有什么用？去掉会怎样？

**答案**：它把用户传入的 `value` **截断到字段宽度**。如果用户误传了一个超过字段宽度的值（例如对 5 位字段传 `0xFFFF`），`& msk` 会把高位多余 bits 切掉，只保留低位 5 位，从而**保护相邻字段不被污染**。去掉它，超宽的高位会被一起左移进寄存器，覆盖到相邻字段，导致寄存器状态错乱。

**练习 2**：`RegGetField` 为什么不需要写回（不是 RMW），而 `RegSetField` 需要？

**答案**：`RegGetField` 只是「读」寄存器并把某字段右移、掩码后返回，不改变寄存器内容，所以读一次就够，无需写回。`RegSetField` 要「改」寄存器里的某几位，但只能整字写（MMIO 写是 32 位的，没有「只写 5 位」的硬件能力），所以必须先读回整字、在内存里改、再整字写回，即 RMW。

**练习 3**：头文件给 `PsiMsDaq_RegWrite`/`RegSetField`/`RegSetBit` 都加了「This function should only be used for debugging purposes! Otherwise the driver might not work.」的警告（见 [psi_ms_daq.h:L628-L629](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L628-L629) 等）。为什么应用层直接用它们「可能让驱动工作异常」？

**答案**：驱动在很多地方做了**软件缓存**以保持与硬件一致——例如 `Str_Configure` 把 `winSize`/`bufStart`/`postTrig`/`windows`/`widthBytes` 缓存进 `PsiMsDaq_StrInst_t`（u3-l4），窗口去环绕、preTrig 计算都依赖这些缓存。如果应用层绕过驱动、用 `RegSetField` 直接改了 `SCFG` 或 `WINSIZE` 寄存器，硬件值变了但软件缓存没跟着变，两者脱节；之后驱动按「旧缓存」算地址、读窗口，就会读到错位置、错长度。所以这组函数仅供调试观测，正式流程必须走 `Str_Configure` 等会同步缓存的 API。

---

### 4.4 返回码全集：PsiMsDaq_RetCode_t 与出错场景速查

#### 4.4.1 概念说明

前面三模块讲了「机制」（`SAFE_CALL` 怎么传播错误、守卫函数与 RMW 助手怎么产生错误）。本模块把这些机制产生的全部错误码汇成一张速查表——`PsiMsDaq_RetCode_t` 枚举，共 12 个码：1 个成功 + 11 个错误。

理解返回码的最佳方式不是死记数字，而是把每个码问三个问题：**「哪个函数会返回它？在什么非法输入或状态下返回？应用层该如何处置？」** 本节就用这张三问表把 12 个码全梳理一遍。

#### 4.4.2 核心流程

返回码可按「错误性质」分成三大族：

1. **静态参数校验族**（调用方传错了常量参数）：`IllegalStrNr`(-1)、`IllegalStrWidth`(-2)、`IllegalWinCnt`(-4)、`IllegalWinNr`(-5)、`WinSizeMustBeMultipleOfSamples`(-10)。这些通常在配置/取句柄阶段就失败，属于编程错误，应在开发期消除。

2. **动态状态/时序校验族**（参数本身合法，但当前状态不允许该操作）：`StrNotDisabled`(-3)、`NoTrigInWin`(-6)、`IrqSchemesWinAndStrAreExclusive`(-11)。这些是运行时契约，调用方需要调整调用顺序或前置条件后重试。

3. **回读容量校验族**（只在 `GetDataUnwrapped` 内出现）：`BufferTooSmall`(-7)、`MorePostTrigThanConfigured`(-8)、`MorePreTrigThanAvailable`(-9)。这三个保护用户缓冲区不被越界写入、并约束请求量不超过配置量。

`SAFE_CALL` 的存在使得这些码会**原样透传**：底层函数返回 `-6`，中间函数（如 `GetDataUnwrapped`）经 `SAFE_CALL` 也返回 `-6`，应用层最终拿到的就是 `-6`，定位起来很直接。

#### 4.4.3 源码精读

枚举定义本身在头文件——[psi_ms_daq.h:L288-L301](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L288-L301)：

```c
typedef enum {
	PsiMsDaq_RetCode_Success	= 0,
	PsiMsDaq_RetCode_IllegalStrNr = -1,
	PsiMsDaq_RetCode_IllegalStrWidth = -2,
	PsiMsDaq_RetCode_StrNotDisabled = -3,
	PsiMsDaq_RetCode_IllegalWinCnt = -4,
	PsiMsDaq_RetCode_IllegalWinNr = -5,
	PsiMsDaq_RetCode_NoTrigInWin = -6,
	PsiMsDaq_RetCode_BufferTooSmall = -7,
	PsiMsDaq_RetCode_MorePostTrigThanConfigured = -8,
	PsiMsDaq_RetCode_MorePreTrigThanAvailable = -9,
	PsiMsDaq_RetCode_WinSizeMustBeMultipleOfSamples = -10,
	PsiMsDaq_RetCode_IrqSchemesWinAndStrAreExclusive = -11
} PsiMsDaq_RetCode_t;
```

把每个码对应到「产生它的函数与触发条件」，得到下表（行号指向产生该码的源码位置）：

| 值 | 名称 | 产生函数（源码位置） | 触发条件 |
|---|---|---|---|
| 0 | `Success` | 几乎所有函数的末尾 `return` | 正常完成，无错误 |
| -1 | `IllegalStrNr` | `CheckStrNr`（[L83](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L83)） | `streamNr >= maxStreams`，流号越界 |
| -2 | `IllegalStrWidth` | `PsiMsDaq_Str_Configure`（[L273](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L273)） | `streamWidthBits % 8 != 0`，流宽不是字节倍数 |
| -3 | `StrNotDisabled` | `CheckStrDisabled`（[L73](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L73)） | 该流在 `STRENA` 中已置位（未先禁用就配置） |
| -4 | `IllegalWinCnt` | `PsiMsDaq_Str_Configure`（[L276](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L276)） | `winCnt > maxWindows`，窗口数超过 IP 上限 |
| -5 | `IllegalWinNr` | `CheckWinNr`（[L93](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L93)） | `winNr >= 该流的 windows`，窗口号越界 |
| -6 | `NoTrigInWin` | `GetPreTrigSamples`（[L542](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L542)）/ `GetTimestamp`（[L566](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L566)） | `WINCNT.ISTRIG == 0`，窗口不含触发 |
| -7 | `BufferTooSmall` | `GetDataUnwrapped`（[L596](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L596)） | `bufferSize < bytes`，用户缓冲区装不下要读的字节 |
| -8 | `MorePostTrigThanConfigured` | `GetDataUnwrapped`（[L599](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L599)） | `postTrigSamples > str_p->postTrig`，请求 postTrig 超过配置 |
| -9 | `MorePreTrigThanAvailable` | `GetDataUnwrapped`（[L602](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L602)） | `preTrigSamples > 实际可用 preTrig`，请求 preTrig 超过窗口实有 |
| -10 | `WinSizeMustBeMultipleOfSamples` | `PsiMsDaq_Str_Configure`（[L279](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L279)） | `winSize % (streamWidthBits/8) != 0`，窗口字节数非采样倍数 |
| -11 | `IrqSchemesWinAndStrAreExclusive` | `SetIrqCallbackWin`（[L343](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L343)）/ `SetIrqCallbackStr`（[L360](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L360)） | 已注册另一种 IRQ 回调（两方案互斥，见 u4-l2） |

**关键细节：`Str_Configure` 的校验顺序**。这张表里 `-2 / -4 / -10 / -3` 四个码都可能在 `Str_Configure` 中返回，但它们有严格的先后——[psi_ms_daq.c:L273-L282](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L273-L282)：

```c
if (0 != (config_p->streamWidthBits % 8))   return PsiMsDaq_RetCode_IllegalStrWidth;   // 第1关：-2
if (config_p->winCnt > ipInst_p->maxWindows) return PsiMsDaq_RetCode_IllegalWinCnt;     // 第2关：-4
if (0 != (config_p->winSize % (config_p->streamWidthBits/8)))
                                            return PsiMsDaq_RetCode_WinSizeMustBeMultipleOfSamples; // 第3关：-10
SAFE_CALL(CheckStrDisabled(ipHandle, strNr));                                              // 第4关：-3
```

顺序是「流宽 → 窗口数上限 → 窗口大小对齐 → 流是否已禁用」。这意味着：**如果同时有多种非法输入，只有排在最前面的那个错误码会被返回**。例如对一个「已使能」的流传 `streamWidthBits=12`，返回的是 `-2`（流宽先判），不是 `-3`（流未禁用）——因为流宽校验在前。这一点对调试很重要：修掉一个返回码后要重新检查，可能暴露下一个潜伏的错误。

> **一个值得深思的边界**：第 3 关 `winSize % (streamWidthBits/8)` 用 `streamWidthBits/8` 做除数。若 `streamWidthBits=0`：第 1 关 `0 % 8 == 0` 通过，但第 3 关会变成 `winSize % 0`——对 0 取模是未定义行为（通常引发除零异常）。驱动的字段类型 `streamWidthBits` 是 `uint16_t`，语言层面无法排除 0。这是源码里一个潜在的边界陷阱：调用方有责任保证 `streamWidthBits > 0`。本讲不把它列为「确定的 bug」，而是提醒你在阅读与扩展时注意这类「校验顺序与隐含约束」的交互。

#### 4.4.4 代码实践

**目标**：用本讲的「校验顺序」知识，预测三种调用场景下 `PsiMsDaq_Str_Configure` 的返回码。这是本讲规格指定的实践任务。

**场景与步骤**：

设某 IP `maxStreams=4`、`maxWindows=8`，流 0 已通过 `SetEnable(str, true)` 使能。逐个回答：

1. **场景 A**：对这条**已使能**的流 0 调 `PsiMsDaq_Str_Configure`，传入一个**完全合法**的 `cfg`（`streamWidthBits=16`、`winCnt=4`、`winSize=64`、其余合法）。预期返回哪个码？
2. **场景 B**：把 `cfg.streamWidthBits` 改成 `12`（其余仍合法，流状态不限）。预期返回哪个码？
3. **场景 C**：`streamWidthBits=16`（合法），但 `winSize=33`、采样大小 `16/8=2` 字节。预期返回哪个码？

**参考答案（源码推导）**：

- **场景 A → `-3 StrNotDisabled`**。前三关都通过（16%8=0；4≤8；64%2=0），走到第 4 关 `SAFE_CALL(CheckStrDisabled(...))`：流 0 在 `STRENA` 第 0 位为 1，`CheckStrDisabled` 返回 `-3`，`SAFE_CALL` 透传，`Str_Configure` 返回 `-3`。处置：调用方应先 `SetEnable(str, false)` 再配置。

- **场景 B → `-2 IllegalStrWidth`**。第 1 关 `12 % 8 = 4 ≠ 0`，**立刻**返回 `-2`，根本不会走到第 4 关。注意：即使该流是「已使能」状态，返回的也是 `-2` 而非 `-3`，因为流宽校验在前。这也说明：不要因为「我明明先禁用了流」就以为 `Configure` 一定不会因宽度失败——宽度是更靠前的独立校验。

- **场景 C → `-10 WinSizeMustBeMultipleOfSamples`**。第 1 关 16%8=0 通过；第 2 关 winCnt 合法通过；第 3 关 `33 % (16/8) = 33 % 2 = 1 ≠ 0`，返回 `-10`。处置：把 `winSize` 改成 2 的倍数（如 32 或 64）。

**需要观察的现象 / 预期结果**：
- 三种场景的返回码分别为 `-3`、`-2`、`-10`，互不相同，对应 4.4.3 表中的三个不同行。
- 关键规律：当多种非法条件并存时，**最靠前的校验关决定返回码**；修好一个后应重新调用，看是否暴露下一个。
- 以上为**源码静态推导**。若要在 ZCU102 参考设计上实测这三个返回码，需构造对应输入并打印 `PsiMsDaq_Str_Configure` 的返回值，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`PsiMsDaq_StrWin_GetDataUnwrapped` 内部第一步就 `SAFE_CALL(PsiMsDaq_StrWin_GetPreTrigSamples(...))`。如果窗口不含触发，`GetDataUnwrapped` 最终返回什么码？为什么？

**答案**：返回 `-6 NoTrigInWin`。因为 `GetPreTrigSamples` 在窗口 `ISTRIG=0` 时返回 `-6`（[L542-L543](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L542-L543)），`GetDataUnwrapped` 开头的 `SAFE_CALL` 把该码短路透传出去（u4-l3 也讨论过这点）。所以 `GetDataUnwrapped` 隐式要求窗口含触发——它不适合处理无触发窗口。

**练习 2**：先调 `PsiMsDaq_Str_SetIrqCallbackWin(str, cb, arg)` 成功，再对同一条流调 `PsiMsDaq_Str_SetIrqCallbackStr(str, cb2, arg)`。第二次调用返回什么？

**答案**：返回 `-11 IrqSchemesWinAndStrAreExclusive`。`SetIrqCallbackStr` 在 [L360-L361](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L360-L361) 检查 `inst_p->irqFctWin != NULL`（因为上一次已注册窗口回调），发现两种方案冲突，返回 `-11`。要切换方案，需先把原回调注销（传 `NULL`）再注册新的（见 u4-l2 的两方案互斥契约）。

**练习 3**：应用层如何用返回码区分「我传错了参数（编程错误）」和「我调用时机不对（运行时契约）」？

**答案**：对照 4.4.2 的三族分类。静态参数校验族（`-1/-2/-4/-5/-10`）意味着传入的常量参数本身非法，是编程错误，应在开发期修掉调用代码；动态状态/时序族（`-3/-6/-11`）意味着参数合法但当前状态不允许，需要调整调用顺序或前置条件（如先禁用流、确保窗口含触发、先注销旧 IRQ 回调）后重试；回读容量族（`-7/-8/-9`）是 `GetDataUnwrapped` 专属，提示用户缓冲区大小或请求量需要调整。把返回码归到这三族，就能快速判断是「改代码」还是「改运行时序列」。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「追踪一个错误码的完整冒泡路径」的综合练习。

**场景**：某应用代码如下（伪代码，仅作说明，非项目原有代码）：

```c
// 示例代码（非项目原有）
PsiMsDaq_IpHandle ip = PsiMsDaq_Init(base, 4, 8, NULL);
PsiMsDaq_StrHandle str;
PsiMsDaq_GetStrHandle(ip, 0, &str);
PsiMsDaq_Str_SetEnable(str, true);          // 把流 0 使能

PsiMsDaq_StrConfig_t cfg = {
    .postTrigSamples = 5,
    .recMode = PsiMsDaqn_RecMode_TriggerMask,
    .winAsRingbuf = true,
    .winOverwrite = false,
    .winCnt = 4,
    .bufStartAddr = 0x40000000,
    .winSize = 64,
    .streamWidthBits = 16,
};
PsiMsDaq_RetCode_t rc = PsiMsDaq_Str_Configure(str, &cfg);   // 此时流 0 仍处于使能
```

**任务**：

1. 指出 `rc` 的值与名称。
2. 完整追踪这个错误码的「冒泡路径」：它最初在哪个函数的哪一行产生？经过哪些函数的 `SAFE_CALL` 逐层透传，最终被应用层收到？画出这条调用链。
3. 沿这条路径，每一层函数各自「做了一步什么」就把码传上去了？
4. 给出修复建议：在调用 `Str_Configure` 前应补一句什么？
5. 进阶：如果把 `cfg.streamWidthBits` 同时改成 `12`，`rc` 会变成什么？为什么不再是第 1 问的码？这说明了本讲的哪条规律？

**参考推演**：

1. `rc = -3`，名称 `PsiMsDaq_RetCode_StrNotDisabled`。
2. 冒泡路径（自底向上）：
   - **产生**：`CheckStrDisabled`（[L73-L74](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L73-L74)）读 `STRENA`，发现 bit0 为 1，`return PsiMsDaq_RetCode_StrNotDisabled`。
   - **第一跳**：`PsiMsDaq_Str_Configure`（[L282](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L282)）的 `SAFE_CALL(CheckStrDisabled(...))` 捕获该返回值，因 `r != Success` 执行 `return r`，把 `-3` 返回给应用层。
   - **到达应用层**：应用层的 `rc` 收到 `-3`。
   - 调用链：`CheckStrDisabled` →（`SAFE_CALL` 透传）→ `PsiMsDaq_Str_Configure` →（`return`）→ 应用层。
3. 每一层做的事：`CheckStrDisabled` 负责「读硬件 + 判断」，是码的**源头**；`Str_Configure` 并没有自己判断，它只是用 `SAFE_CALL` 把下层码**原样转发**（短路），自己不生产新码；应用层是码的**消费者**，据此决定后续动作。
4. 修复建议：在 `Str_Configure` 之前补一句 `PsiMsDaq_Str_SetEnable(str, false);` 先禁用流 0，满足「先停后改」契约，使 `CheckStrDisabled` 通过，`Str_Configure` 即可正常写寄存器并返回 `Success`(0)。
5. 进阶：若同时把 `streamWidthBits` 改成 `12`，则 `rc` 变成 `-2 IllegalStrWidth`。因为 `Str_Configure` 的第 1 关（[L273](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L273)）流宽校验在第 4 关 `CheckStrDisabled` 之前，`12%8≠0` 直接返回 `-2`，根本走不到 `-3`。这说明了本讲反复强调的规律：**校验有先后，多种非法并存时只有最靠前者决定返回码**；调试时修掉一个码后必须重新检查，可能暴露下一个潜伏错误。

> 以上第 1–5 步为源码静态推导；若要在 ZCU102 参考设计上观测这条冒泡路径与各返回码，**待本地验证**。

## 6. 本讲小结

- **`SAFE_CALL`**（[L44-L46](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L44-L46)）把「调用 → 检查 → 失败即返回」三步压成一行，实现错误码沿调用栈的短路传播；它只能用于返回 `PsiMsDaq_RetCode_t` 的函数，宏体的 `{ }` 给局部变量 `r` 独立作用域，使同一函数可连写多个。
- **三个守卫函数**：`CheckStrNr`（流号 vs `maxStreams`）、`CheckWinNr`（窗口号 vs **该流的** `windows`）、`CheckStrDisabled`（读 `STRENA` 判流是否已停）。前两者纯内存比较，第三个读硬件；它们分别对应 `-1`、`-5`、`-3` 三个码。
- **四个 RMW 助手**：`RegSetField`/`RegGetField` 按 `[msb:lsb]` 字段操作，掩码 `msk=(1<<(msb+1))-1`；`RegSetBit`/`RegGetBit` 按现成掩码操作。写类助手做「读改写」，是同一寄存器多子字段增量写入（如 `SCFG` 的 `RINGBUF`/`OVERWRITE`/`WINCNT` 三次写）互不破坏的根本保障。
- **`PsiMsDaq_RetCode_t`** 共 12 个码，分三族：静态参数校验族（`-1/-2/-4/-5/-10`）、动态状态/时序族（`-3/-6/-11`）、回读容量族（`-7/-8/-9`）。把码归族即可快速判断要「改代码」还是「改调用时序」。
- **校验顺序决定返回码**：`Str_Configure` 按「流宽 → 窗口数上限 → 窗口对齐 → 流已禁用」四关依次检查，多种非法并存时只有最靠前者返回；调试时修一个码后要复检下一个。
- **高级调试函数的风险**：`PsiMsDaq_RegWrite`/`RegRead`/`RegSetField`/`RegSetBit` 虽然公开暴露，但绕过驱动会破坏软件缓存与硬件的一致性（如 `winSize`/`postTrig` 缓存），头文件反复警告「仅供调试」。

## 7. 下一步学习建议

- **横向收口：驱动底层已讲完**。本讲是 u3-l2/u3-l3/u4-l2/u4-l3/u4-l4 这条「驱动实现」主线的收尾。建议回头快速通读一遍 [psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) 全文，验证现在每一个 `SAFE_CALL`、每一个 RMW、每一个 `return PsiMsDaq_RetCode_*` 你都能说清来历——这是检验是否真正掌握驱动的试金石。
- **进入端到端集成**：下一单元（u5）把本讲（以及前几讲）的 IP、驱动、中断、数据回读放到 ZCU102 参考设计里串成一条完整链路。建议先读 u5-l1（Vivado 工程与时钟域），再读 u5-l2（[main.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/Sdk/app/src/main.c) 的端到端 C 应用），看真实代码如何检查 `PsiMsDaq_Str_Configure` 等函数的返回码、如何在中断回调里调 `GetDataUnwrapped` + `MarkAsFree`。
- **向上追硬件实现**：本讲的 `STRENA`/`SCFG`/`WINCNT` 等寄存器与 `ISTRIG`/`REC`/`ARM` 等比特都是上游 `psi_multi_stream_daq` 的 IP-Core 硬件定义的（本仓库只是 Vivado 封装层，见 u1-l1）。若你想知道这些字段在硬件侧如何被读写、状态机如何流转，需要去上游仓库阅读 `psi_ms_daq_axi` 的 RTL 与 PDF 文档。
- **动手验证建议**：在 ZCU102 参考设计里故意制造本讲「综合实践」的错误场景（对使能流调 `Str_Configure`、传非字节倍数的流宽、传非采样倍数的 `winSize`），打印返回码并与本讲预测对照；再尝试用 `PsiMsDaq_RegSetField` 直接读改一个寄存器观察驱动行为异常，亲身感受「仅供调试」警告的含义。所有硬件实验均**待本地验证**。
