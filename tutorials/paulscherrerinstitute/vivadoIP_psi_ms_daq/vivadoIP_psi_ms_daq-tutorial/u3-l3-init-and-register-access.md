# 初始化与寄存器访问抽象

## 1. 本讲目标

上一讲（u3-l2）我们建立了驱动的「软件数据模型」：两个不透明句柄 `PsiMsDaq_IpHandle` / `PsiMsDaq_StrHandle`、两个私有结构体 `PsiMsDaq_Inst_t` / `PsiMsDaq_StrInst_t`，以及可注入的访问函数指针。本讲要回答两个紧接着的问题：

1. **这些访问函数指针到底指向什么？** 默认实现长什么样？为什么默认实现能直接在 Zynq 裸机上跑？
2. **`PsiMsDaq_Init` 作为驱动唯一构造函数，到底做了哪些事？** 它如何把一块刚上电、状态未知的 IP-Core 拉到一个「干净、全局使能、但所有流仍关闭」的已知状态？

学完本讲你应当能：

- 画出一次寄存器读/写从高层 API 到物理总线的完整跳转路径（两层间接）。
- 说清楚 `volatile` 关键字、`(size_t)` 中间强转、`baseAddr + addr` 地址叠加这三者各自的作用。
- 解释 `strAddrOffs = Pow(2, Log2Ceil(maxWindows)) * 0x10` 的含义，以及为什么 `maxWindows` 必须是 2 的幂。
- 默写出 `PsiMsDaq_Init` 的复位序列（禁能 → 清状态 → 末尾置全局使能位）。
- 知道在 Linux 用户态或仿真环境里，如何通过 `accessFct_p` 注入自定义读写函数让同一份驱动源码跨平台运行。

## 2. 前置知识

本讲默认你已经掌握：

- **u3-l1 的寄存器地址模型**：四类地址空间（通用 `0x000`、逐流录制 `0x200`、逐流上下文 `0x1000`、窗口 `0x4000`），所有寄存器宏给出的是「相对 IP 基址的字节偏移」。
- **u3-l2 的句柄与结构体**：`PsiMsDaq_Inst_t` 里那三个字段 `regWrFct` / `regRdFct` / `memcpyFct` 是函数指针；`PsiMsDaq_StrInst_t` 里有 `ipHandle` 回指指针。
- **C 语言函数指针**：`void (*f)(int)` 这种「指向函数的指针」的语法与调用方式。
- **内存映射 I/O（MMIO）**：CPU 用普通的访存指令去读写一块「其实不是内存而是外设寄存器」的地址空间。Zynq 里 AXI Slave 外设就挂在这样的地址上。

如果对最后一条不熟，记住一句话即可：**对 IP 寄存器的读/写，本质就是对某个特定物理地址的 `LDR`/`STR`（汇编里的加载/存储）指令。** 本讲后面所有抽象都是围绕「这个地址怎么算、这条指令怎么发出去」展开的。

## 3. 本讲源码地图

本讲只涉及驱动里的两个文件，且只精读其中与「初始化 + 寄存器访问」直接相关的几个函数：

| 文件 | 角色 | 本讲用到的关键片段 |
|------|------|---------------------|
| `drivers/psi_ms_daq_axi/src/psi_ms_daq.c` | 驱动实现（私有结构体 + 所有函数体） | `PsiMsDaq_DataCopy_Standard`、`PsiMsDaq_RegWrite_Standard`、`PsiMsDaq_RegRead_Standard`、`Log2`、`Log2Ceil`、`Pow`、`PsiMsDaq_Init`、`PsiMsDaq_RegWrite`、`PsiMsDaq_RegRead` |
| `drivers/psi_ms_daq_axi/src/psi_ms_daq.h` | 驱动公共接口（句柄 typedef、函数原型、寄存器宏、返回码） | `PsiMsDaq_RegWrite_f` / `PsiMsDaq_RegRead_f` / `PsiMsDaq_DataCopy_f` 三个函数指针 typedef、`PsiMsDaq_AccessFct_t` 结构体、`PsiMsDaq_Init` 原型、`GCFG`/`IRQVEC`/`IRQENA`/`STRENA` 寄存器宏 |

> 提醒：按 u1-l3 的结论，本地 `drivers/*.c/*.h` 每次打包都会被上游 `psi_multi_stream_daq` 的同名文件覆盖；我们在本仓库读到的就是当前版本的真实代码，但「真身」在上游。

下面按「自底向上」的顺序讲解四个最小模块：先看最底层的裸指针实现（Standard 函数），再看包在它外面的间接层（RegWrite/RegRead），再看 Init 用到的数学辅助函数，最后看 Init 如何把这一切串成一次完整的上电复位。

## 4. 核心概念与源码讲解

### 4.1 默认访问函数 `PsiMsDaq_*_Standard`：最底层的裸指针实现

#### 4.1.1 概念说明

驱动要读/写 IP 寄存器，最终都必须落到「对某个地址发一条访存指令」。但「怎么发这条指令」在不同运行环境下完全不同：

- **Zynq 裸机（baremetal）**：CPU 看到的就是物理地址，直接把地址强转成指针解引用即可。
- **Linux 用户态**：进程跑在虚拟地址空间，物理地址不能直接解引用，必须先 `mmap("/dev/mem")` 把物理地址映射进进程。
- **仿真（VHDL testbench + C 驱动）**：根本没有真实内存，一次「写寄存器」要翻译成对 DUT 信号的一次 poke。

驱动作者无法预知你最终跑在哪种环境，于是把「真正发访存指令」这件事抽成三个**可替换**的函数，并贴心地提供一份**默认实现** `PsiMsDaq_*_Standard`，覆盖最常见的裸机场景。这份默认实现就是本节要讲的「最底层」。

#### 4.1.2 核心流程

三个默认函数的职责与签名（来自头文件的 typedef）：

| 函数 | 签名（typedef） | 干什么 |
|------|------------------|--------|
| `PsiMsDaq_RegWrite_Standard` | `void (const uint32_t addr, const uint32_t value)` | 把 `value` 写入地址 `addr` |
| `PsiMsDaq_RegRead_Standard` | `uint32_t (const uint32_t addr)` | 读出地址 `addr` 处的 32 位值并返回 |
| `PsiMsDaq_DataCopy_Standard` | `void (void* dst, void* src, size_t n)` | 从 `src` 拷 `n` 字节到 `dst`（就是 `memcpy` 的薄包装） |

读写函数的共同套路只有三步：

1. 把整数地址 `addr` 强转成「指向 32 位寄存器」的指针。
2. 对该指针解引用（读）或赋值（写）。
3. 编译器保证这条解引用不被优化掉（靠 `volatile`）。

#### 4.1.3 源码精读

三个 typedef 的定义在头文件里（注意它们定义的是**函数类型**，加 `*` 才是函数指针）：

[psi_ms_daq.h:L201-L217](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L201-L217) — 三个访问函数的类型签名：`DataCopy_f(dst, src, n)`、`RegWrite_f(addr, value)`、`RegRead_f(addr) -> uint32_t`。

默认实现在 `.c` 文件顶部，紧挨着结构体定义之后：

```c
void PsiMsDaq_RegWrite_Standard(const uint32_t addr, const uint32_t value)
{
	volatile uint32_t* addr_p = (volatile uint32_t *)(size_t)addr;
	*addr_p = value;
}

uint32_t PsiMsDaq_RegRead_Standard(const uint32_t addr)
{
	volatile uint32_t* addr_p = (volatile uint32_t *)(size_t)addr;
	return *addr_p;
}
```

[psi_ms_daq.c:L51-L66](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L51-L66) — 默认访问函数：`DataCopy_Standard` 直接 `memcpy`，`RegWrite_Standard`/`RegRead_Standard` 用 `volatile` 指针解引用。

逐字逐句拆这条语句 `volatile uint32_t* addr_p = (volatile uint32_t *)(size_t)addr;`：

- **`(size_t)addr`**：先把 `uint32_t` 的地址值扩成 `size_t`（与目标平台指针等宽的无符号整数）。这是为了在 64 位主机上避免「int → 指针」的窄化警告；在 32 位 Zynq 上 `size_t` 也是 32 位，等于无事发生。
- **`(volatile uint32_t *)`**：再把整数解释成「指向 32 位寄存器」的指针。注意这里的 **`volatile`**——它告诉编译器：「这个地址背后的值可能在你不知道的时候变化（因为是硬件寄存器），每次都要真正发出访存指令，不许缓存到寄存器、不许把连续两次访问合并成一次」。**没有 `volatile`，MMIO 几乎一定坏掉**（编译器可能把一次「读状态寄存器直到就绪」的死循环优化成只读一次）。
- **`*addr_p = value;` / `return *addr_p;`**：解引用即对应一条 `STR`/`LDR` 汇编指令，AXI 互联把它路由到挂在该地址的 IP 寄存器。

`PsiMsDaq_DataCopy_Standard` 只是标准库 `memcpy` 的包装，没有任何花活——它用于 4.3 节以外的窗口数据回读（`PsiMsDaq_StrWin_GetDataUnwrapped`），本讲不展开。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认 `volatile` 不是装饰，而是 MMIO 正确性的关键。

**操作步骤**：

1. 打开 [psi_ms_daq.c:L56-L60](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L56-L60)。
2. 在脑子里把 `volatile` 去掉，想象 `PsiMsDaq_RegRead_Standard` 被这样调用（轮询状态寄存器的某 bit）：

   ```c
   /* 示例代码：仅用于说明，不是项目原有代码 */
   uint32_t v;
   do {
       PsiMsDaq_RegRead_Standard(STATUS_REG);   /* 假设无 volatile */
   } while ((v & READY_BIT) == 0);
   ```

**需要观察的现象**：去掉 `volatile` 后，编译器可能认为「这个地址在循环里没人改、函数又没有副作用」，于是只读一次寄存器、把结果缓存，循环要么立刻退出（误判就绪）、要么永远不退出（看不到硬件置位）。

**预期结果**：理解为什么所有 MMIO 指针都必须带 `volatile`——它强制每次解引用都生成真实的访存指令。

**待本地验证**：如果你手头有交叉编译工具链，可以把 `volatile` 删掉后用 `-O2` 编译，对比汇编里 `LDR` 出现的次数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `addr` 参数类型是 `uint32_t` 而不是 `void*` 或 `uintptr_t`？

**答案**：因为 IP 寄存器地址本身是个**数值**（来自 `baseAddr + 偏移` 的算术结果），用整数类型方便做加法（4.2 节会看到 `inst_p->baseAddr + addr`）。如果用指针类型，每次算偏移都要先强转成整数、算完再转回指针，反而啰嗦。在 32 位 Zynq 上 `uint32_t` 与指针等宽，无损。

**练习 2**：`PsiMsDaq_DataCopy_Standard` 为什么不直接在外部调用 `memcpy`，而要包一层？

**答案**：为了和 `RegWrite`/`RegRead` 一起放进 `PsiMsDaq_AccessFct_t` 这个「可替换访问函数集」里（4.2 节）。在 Linux 上，DMA 写入的 DDR 物理地址同样需要先 `mmap` 才能被 CPU 访问，这时数据拷贝也要走自定义实现，签名必须统一。

---

### 4.2 寄存器读写间接层 `PsiMsDaq_RegWrite` / `RegRead`：叠加 baseAddr 并分发

#### 4.2.1 概念说明

4.1 节的 Standard 函数只认「绝对地址」，但驱动高层 API（如 `PsiMsDaq_Str_Configure`）手里只有「相对偏移」（来自 u3-l1 的寄存器宏，比如 `PSI_MS_DAQ_REG_MODE(2)` 算出来是 `0x208+0x10*2 = 0x228`）。谁来把「相对偏移」变成「绝对地址」？谁来决定用默认实现还是用户注入的实现？

答案就是本节的两个间接层函数 `PsiMsDaq_RegWrite` / `PsiMsDaq_RegRead`。它们是**所有**高层寄存器访问的必经之路，承担两件事：

1. 把句柄（`void*`）还原成 `PsiMsDaq_Inst_t*`，拿到 `baseAddr` 与函数指针。
2. 算出绝对地址 `baseAddr + addr`，再调用注入的（或默认的）访问函数。

正因为有这一层，4.4 节的 `PsiMsDaq_Init` 才能在「还没设置完访问函数」与「设置完之后」用同一套寄存器写操作完成复位。

#### 4.2.2 核心流程

```
高层 API:  PsiMsDaq_RegWrite(ipHandle, OFFSET, value)
              │
              ▼  把 void* 强转回 PsiMsDaq_Inst_t*
              │
              ▼  计算 abs = inst_p->baseAddr + OFFSET
              │
              ▼  调用 inst_p->regWrFct(abs, value)   ← 函数指针分发
              │
              ▼  默认指向 PsiMsDaq_RegWrite_Standard（裸指针解引用）
                 或指向用户注入的函数（mmap / 仿真后门）
```

`RegRead` 完全对称，只是数据流向反过来。两者都返回 `PsiMsDaq_RetCode_Success`——这一层本身不产生错误（它只是转发），错误检查发生在更上层的守卫函数里（见 u4-l4）。

#### 4.2.3 源码精读

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegWrite(	PsiMsDaq_IpHandle ipHandle,
										const uint32_t addr,
										const uint32_t value)
{
	PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*)ipHandle;   /* 还原结构体 */
	inst_p->regWrFct(inst_p->baseAddr+addr, value);          /* 地址叠加 + 函数指针分发 */
	return PsiMsDaq_RetCode_Success;
}

PsiMsDaq_RetCode_t PsiMsDaq_RegRead(	PsiMsDaq_IpHandle ipHandle,
										const uint32_t addr,
										uint32_t* const value_p)
{
	PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*)ipHandle;
	*value_p = inst_p->regRdFct(inst_p->baseAddr+addr);      /* 读：把返回值写回 out 参数 */
	return PsiMsDaq_RetCode_Success;
}
```

[psi_ms_daq.c:L730-L752](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L730-L752) — 寄存器读写间接层：把句柄还原成结构体、叠加 `baseAddr`、通过函数指针 `regWrFct`/`regRdFct` 分发到具体实现。

三个要点：

- **`(PsiMsDaq_Inst_t*)ipHandle`**：这就是 u3-l2 讲过的「不透明指针」拆箱——外界只看到 `void*`，驱动内部一强转就拿到了真实的结构体，从而能读 `baseAddr` 和函数指针字段。
- **`inst_p->baseAddr + addr`**：`baseAddr` 是 4.4 节 Init 时记下的 IP 物理基址（在 ZCU102 上来自 `xparameters.h` 的 `XPAR_..._BASEADDR`），`addr` 是 u3-l1 寄存器宏算出的偏移。两者相加才是 Standard 函数需要的绝对地址。
- **`inst_p->regWrFct(...)`**：注意这是**通过函数指针调用函数**，而不是直接调 `PsiMsDaq_RegWrite_Standard`。Init 时把 `regWrFct` 设成哪个，这里就执行哪个——这就是「可注入」的实现机制。

头文件里这两个函数的原型与「仅供调试」的告警：

[psi_ms_daq.h:L631-L645](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L631-L645) — `PsiMsDaq_RegWrite`/`RegRead` 原型，注释明确「只应做调试用途，否则驱动可能工作不正常」，因为绕过它直接写寄存器会破坏驱动的软件状态缓存。

> 注意：头文件把 `RegWrite`/`RegRead` 标成「调试用途」是对**外部应用**说的（应用不该绕过 `Str_*` 高层 API 直接戳寄存器）。而驱动**内部**所有寄存器操作恰恰都走这两个函数——它们是内部基础设施，不是给应用随便用的。

#### 4.2.4 代码实践（源码阅读型 + 跟踪调用链）

**实践目标**：确认「所有寄存器操作都经过间接层」这条断言，并理解地址叠加。

**操作步骤**：

1. 打开 `PsiMsDaq_Str_ClrMaxLvl`（[psi_ms_daq.c:L409-L419](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L409-L419)），看它调用 `PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_MAXLVL(strNr), 0)`。
2. 查 u3-l1 的寄存器宏 `PSI_MS_DAQ_REG_MAXLVL(n) = 0x200 + 0x10*n`（[psi_ms_daq.h:L155](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L155)）。
3. 假设 `baseAddr = 0x80000000`（ZCU102 上某个 AXI Slave 基址）、`strNr = 2`，手算最终落到 Standard 函数的绝对地址。

**需要观察的现象 / 预期结果**：

- 偏移 = `0x200 + 0x10*2 = 0x220`。
- 绝对地址 = `0x80000000 + 0x220 = 0x80000220`。
- 最终 `PsiMsDaq_RegWrite_Standard` 会向 `0x80000220` 写 `0`。

**待本地验证**：在真实工程里用 Vivado 地址编辑器核对 IP 的 AXI Slave 基址，再对照 `xparameters.h` 里的 `XPAR_PSI_MS_DAQ_AXI_BASEADDR` 是否一致。

#### 4.2.5 小练习与答案

**练习 1**：如果应用绕过 `PsiMsDaq_Str_*` API，直接调 `PsiMsDaq_RegWrite` 改了某流的窗口数，会发生什么？

**答案**：硬件寄存器被改了，但驱动结构体 `PsiMsDaq_StrInst_t` 里的软件缓存字段（如 `windows`、`widthBytes`）不会跟着更新。后续窗口地址计算（依赖 `windows`）就会用旧值，可能算错地址或越界。这就是头文件把它标成「调试用途」的原因。

**练习 2**：`RegRead` 为什么用 out 参数 `uint32_t* value_p` 而不是直接返回 `uint32_t`？

**答案**：为了和所有其它驱动函数统一返回 `PsiMsDaq_RetCode_t` 错误码。如果 `RegRead` 直接返回 `uint32_t`，就拿不到错误码通道了（虽然当前实现永远返回 Success，但接口形状保持一致便于将来扩展，例如注入版可能返回超时错误）。

---

### 4.3 数学辅助函数 `Log2` / `Log2Ceil` / `Pow`：算出窗口地址的流间步进

#### 4.3.1 概念说明

回顾 u3-l1 的窗口寄存器宏：

```
PSI_MS_DAQ_WIN_WINCNT(n, w, so) = 0x4000 + (so)*n + 0x10*w
```

这里 `so`（stream offset）是「相邻两条流之间窗口区的字节间隔」，即 `strAddrOffs`。一个流有 `maxWindows` 个窗口、每个窗口占 `0x10` 字节（4 个 32 位寄存器），所以「恰好不重叠」需要的步进是 `maxWindows * 0x10`。

驱动没有直接写 `maxWindows * 0x10`，而是写成了 `Pow(2, Log2Ceil(maxWindows)) * 0x10`。为什么要绕这一圈？因为窗口区要求**步进必须是 2 的幂**（这样地址高位可以直接做流号译码），而 `maxWindows` 在 IP 综合时也被约束为 2 的幂。本节的三个小函数就是用来算这个「把 2 的幂还原出来」的。

#### 4.3.2 核心流程

三个函数的定义（注意它们都不依赖任何外部库，纯整数循环）：

```c
Log2(x)      /* 返回 floor(log2(x))，x>=1 */
Log2Ceil(x)  /* x==0 返回 0；否则返回 Log2(x) */
Pow(x, y)    /* 返回 x^y */
```

`strAddrOffs` 的计算链：

\[
\text{strAddrOffs} = \text{Pow}(2,\ \text{Log2Ceil}(\text{maxWindows})) \times \text{0x10}
\]

当 `maxWindows = 2^k`（2 的幂）时：

\[
\text{Log2Ceil}(2^k) = k,\quad \text{Pow}(2, k) = 2^k = \text{maxWindows}
\]

\[
\Rightarrow \text{strAddrOffs} = \text{maxWindows} \times \text{0x10}
\]

这正是「恰好不重叠」所需的最小步进。

#### 4.3.3 源码精读

```c
uint32_t Log2(const uint32_t x)
{
	uint32_t v = x;
	uint32_t r = 0;
	while (v > 1) {
		v = v/2;
		r = r+1;
	}
	return r;
}

uint32_t Log2Ceil(const uint32_t x)
{
	if (0 == x) {
		return 0;
	}
	return Log2(x);
}

uint32_t Pow(const uint32_t x, const uint32_t y)
{
	uint32_t r = x;
	for (uint32_t i = 1; i < y; i ++) {
		r *= x;
	}
	return r;
}
```

[psi_ms_daq.c:L99-L125](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L99-L125) — 三个数学辅助函数：`Log2` 用「反复除 2 数次数」求 floor(log2)，`Log2Ceil` 仅对 0 加保护，`Pow` 用循环连乘。

逐个手算验证：

| 输入 `x` | `Log2(x)` 跟踪过程 | 结果 |
|----------|---------------------|------|
| 1 | `v=1`，循环不进 | 0 |
| 2 | `v=2→1, r=1` | 1 |
| 4 | `v=4→2→1, r=2` | 2 |
| 8 | `v=8→4→2→1, r=3` | 3 |
| 16 | 4 次 | 4 |
| 32 | 5 次 | 5 |

所以 `Log2(2^k) = k`，`Pow(2, k) = 2^k`，链条成立。

**两个值得注意的细节（基于真实代码，非推测）**：

1. **`Log2Ceil` 名不副实**。它的实现里只是对 `x==0` 做了保护，对 `x>=1` 直接返回 `Log2(x)`，而 `Log2` 算的是 **floor**（向下取整）。换句话说，对非 2 的幂（如 `x=3`），`Log2Ceil(3) = Log2(3) = 1`，并不是数学上的 ceil(log2(3)) = 2。这套公式之所以仍然正确，**完全依赖 `maxWindows` 必须是 2 的幂这一外部约束**（此时 floor 与 ceil 相等）。一旦传入 `maxWindows=3`，`strAddrOffs = Pow(2,1)*0x10 = 0x20`，只够放 2 个窗口，第 3 个窗口的地址会和下一条流的窗口 0 重叠——这正是 u3-l1 强调「`maxWindows` 必须为 2 的幂，否则相邻流窗口区会重叠」的代码根因。

2. **`Pow(x, 0)` 返回 `x` 而不是 1**。因为循环从 `i=1` 开始、条件 `i < y`，当 `y=0` 时循环不执行、直接返回初值 `r=x`。对 `maxWindows=1` 的情况，`Log2Ceil(1)=0`、`Pow(2,0)=2`，于是 `strAddrOffs = 0x20`（比实际需要的 `0x10` 大一倍）。这只浪费一点地址空间、不影响正确性，属于可接受的边界行为。

#### 4.3.4 代码实践（手算型）

**实践目标**：用三个真实函数手算 `strAddrOffs`，体会「2 的幂」约束的必要性。

**操作步骤**：

1. 对 `maxWindows = 16`，按 4.3.3 的手算表算出 `Log2Ceil(16) = 4`、`Pow(2,4) = 16`、`strAddrOffs = 16 * 0x10 = 0x100`。
2. 对 `maxWindows = 24`（**非** 2 的幂），算出 `Log2Ceil(24) = Log2(24) = 4`（因为 `24→12→6→3→1` 共 4 次）、`Pow(2,4) = 16`、`strAddrOffs = 0x100`。

**需要观察的现象**：`maxWindows=24` 时实际需要 `24 * 0x10 = 0x180` 才不重叠，但公式只给 `0x100`，于是流 0 的窗口 16~23（地址 `0x4000+0x100` 起）会和流 1 的窗口 0~7（地址 `0x4000+0x100` 起）**撞在一起**。

**预期结果**：直观理解「为什么 `maxWindows` 必须是 2 的幂」——这不是文档里的空话，而是 `Log2Ceil` 实现取 floor 导致的硬性约束。

**待本地验证**：可写一段最小 C 程序（示例代码）把这三个函数抄进去，打印 `maxWindows` 从 1 到 33 时 `strAddrOffs` 与 `maxWindows*0x10` 的差异，观察只有在 2 的幂处二者相等。

#### 4.3.5 小练习与答案

**练习 1**：`Log2Ceil` 想表达的语义是 ceil(log2(x))，但实现是 floor。如果让你修这个函数让它对非 2 的幂也正确（即真正的 ceil），最少怎么改？

**答案**：在 `Log2(x)` 之后判断「x 是否本身就是 2 的幂」：若是直接返回，否则加 1。即 `return (x & (x-1)) ? Log2(x)+1 : Log2(x);`（位运算判 2 的幂：2 的幂只有一个 1，`x & (x-1)` 为 0）。不过本项目靠外部约束保证 `maxWindows` 是 2 的幂，所以没必要改。

**练习 2**：为什么驱动要自己实现 `Pow` 而不用标准库 `pow`？

**答案**：标准库 `pow` 是浮点函数（`double`），在无 FPU 的软核（如 MicroBlaze）或裸机环境里既慢又可能引入浮点链接开销。这里只需整数 2 的幂，自己写一个整数循环更轻量、更可控。

---

### 4.4 `PsiMsDaq_Init`：把上电状态拉到已知态的构造函数

#### 4.4.1 概念说明

`PsiMsDaq_Init` 是驱动唯一的「构造函数」：它接收一块刚上电、状态未知的 IP-Core，返回一个可用的 IP 句柄。它要做的事情远不止 `malloc`——它还要把硬件拉到一个**已知的安全状态**：

- 禁止任何流采集（`STRENA=0`）。
- 禁止任何中断（`IRQENA=0`）并清掉残留的中断（`IRQVEC=0xFFFFFFFF`）。
- 清空每条流的最大水位计（`MAXLVL=0`）和所有窗口计数（`WINCNT=0`）。
- 最后只置两个**全局**使能位 `ENA | IRQENA`，这两个位以后再不动；后续按流粒度的开关交给 `STRENA`/`IRQENA`。

同时它要决定「用默认访问函数还是用户注入的」，并把 `strAddrOffs` 算好缓存起来。可以说本讲前面三节（Standard 函数、RegWrite/RegRead 间接层、Log2/Pow）都是为这一节服务的。

#### 4.4.2 核心流程

`PsiMsDaq_Init(baseAddr, maxStreams, maxWindows, accessFct_p)` 的执行序列：

```
1. 分配与记录身份
   ├─ malloc 一个 PsiMsDaq_Inst_t
   ├─ 记下 baseAddr / maxStreams / maxWindows
   ├─ malloc maxStreams 个 PsiMsDaq_StrInst_t（流实例数组）
   └─ strAddrOffs = Pow(2, Log2Ceil(maxWindows)) * 0x10     ← 4.3 节

2. 装配访问函数（二选一）
   ├─ accessFct_p == NULL → 用三个 *_Standard 默认实现   ← 4.1 节
   └─ accessFct_p != NULL → 用用户注入的 dataCopy/regWrite/regRead

3. 硬件复位（此时 regWrFct 已就绪，可安全调用 RegWrite）  ← 4.2 节
   ├─ GCFG    = 0            （关总使能）
   ├─ STRENA  = 0            （关所有流）
   ├─ IRQENA  = 0            （关所有中断）
   └─ IRQVEC  = 0xFFFFFFFF   （写 1 清除，应答所有残留中断）

4. 逐流逐窗口清状态 + 初始化软件字段
   for str in 0..maxStreams:
   │  ├─ MAXLVL(str) = 0
   │  ├─ for win in 0..maxWindows: WINCNT(str,win) = 0
   │  └─ 软件字段: nr / isConfigured=false / 回调=NULL /
   │              ipHandle=回指 / lastProcWin=-1 / irqCalledWin=0

5. 置全局使能（此后不再动 GCFG）
   └─ GCFG = ENA | IRQENA

6. 返回 (PsiMsDaq_IpHandle) inst_p
```

注意步骤 2 必须在步骤 3 之前——因为步骤 3 的 `PsiMsDaq_RegWrite` 内部要调 `inst_p->regWrFct`，而那个函数指针是步骤 2 装配的。这就是 u3-l2 强调「访问函数在 Init 里一次性装配」的代码依据。

#### 4.4.3 源码精读

```c
PsiMsDaq_IpHandle PsiMsDaq_Init(	const uint32_t baseAddr,
									const uint8_t maxStreams,
									const uint8_t maxWindows,
									const PsiMsDaq_AccessFct_t* const accessFct_p)
{
	/* 1. 分配与记录身份 */
	PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*) malloc(sizeof(PsiMsDaq_Inst_t));
	inst_p->baseAddr = baseAddr;
	inst_p->streams = (PsiMsDaq_StrInst_t*) malloc(sizeof(PsiMsDaq_StrInst_t)*maxStreams);
	inst_p->maxWindows = maxWindows;
	inst_p->maxStreams = maxStreams;
	inst_p->strAddrOffs = Pow(2, Log2Ceil(maxWindows))*0x10;   /* ← 4.3 */

	/* 2. 装配访问函数 */
	if (NULL == accessFct_p) {
		inst_p->memcpyFct = PsiMsDaq_DataCopy_Standard;
		inst_p->regWrFct  = PsiMsDaq_RegWrite_Standard;
		inst_p->regRdFct  = PsiMsDaq_RegRead_Standard;
	} else {
		inst_p->memcpyFct = accessFct_p->dataCopy;
		inst_p->regWrFct  = accessFct_p->regWrite;
		inst_p->regRdFct  = accessFct_p->regRead;
	}

	/* 3. 硬件复位（禁能 + 清残留中断） */
	PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_GCFG,   0);
	PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_STRENA, 0);
	PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_IRQENA, 0);
	PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_IRQVEC, 0xFFFFFFFF);

	/* 4. 逐流逐窗口清状态 + 初始化软件字段 */
	for (int str = 0; str < maxStreams; str++) {
		PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_MAXLVL(str), 0);
		for (int win = 0; win < maxWindows; win++) {
			PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_WIN_WINCNT(str, win, inst_p->strAddrOffs), 0);
		}
		inst_p->streams[str].nr = str;
		inst_p->streams[str].isConfigured = false;
		inst_p->streams[str].irqFctWin = NULL;
		inst_p->streams[str].irqFctStr = NULL;
		inst_p->streams[str].irqArg = NULL;
		inst_p->streams[str].ipHandle = (PsiMsDaq_IpHandle) inst_p;   /* 流回指 IP */
		inst_p->streams[str].lastProcWin = -1;
		inst_p->streams[str].irqCalledWin = 0;
	}

	/* 5. 置全局使能（此后不再动） */
	PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_GCFG,
	                  PSI_MS_DAQ_REG_GCFG_BIT_ENA | PSI_MS_DAQ_REG_GCFG_BIT_IRQENA);
	return (PsiMsDaq_IpHandle) inst_p;
}
```

[psi_ms_daq.c:L132-L181](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L132-L181) — `PsiMsDaq_Init` 全文：分配 → 装配访问函数 → 硬件复位 → 逐流初始化 → 置全局使能 → 返回句柄。

几处关键细节：

- **`malloc` 不检查返回值**。如果 `malloc` 返回 `NULL`（内存不足），紧接着的 `inst_p->baseAddr = ...` 会空指针解引用崩溃。驱动假定裸机环境堆内存充足、`malloc` 必成功——这是一个真实的设计取舍，使用时心里有数。
- **`accessFct_p` 的二选一**。传 `NULL` 走默认（裸机直访），传非 `NULL` 走用户实现。`PsiMsDaq_AccessFct_t` 结构体把三个函数指针打包在一起（[psi_ms_daq.h:L279-L283](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L279-L283)），这样一次调用就能换全套访问层。
- **`IRQVEC = 0xFFFFFFFF` 的含义**。`IRQVEC` 是「位=流」编码、**写 1 清除**（u3-l1 讲过）：写全 1 就是把所有流可能残留的中断全部应答掉，确保上电后不会立刻收到一个「假」中断。
- **`ipHandle = (PsiMsDaq_IpHandle) inst_p`**。这就是 u3-l2 讲的「回指指针」：每个流实例都记下自己属于哪个 IP 实例，这样流级 API（如 `PsiMsDaq_Str_Configure`）才能从流句柄一路找回 `baseAddr` 与访问函数。
- **`GCFG = ENA | IRQENA` 放在最后**。`ENA`（bit0）是 IP 全局使能，`IRQENA`（bit8）是全局中断使能（见 [psi_ms_daq.h:L147-L149](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L147-L149)）。注释「never touched later」很关键：这两个全局位此后驱动再也不写 `GCFG`，按流粒度的开关完全靠 `STRENA`（流使能）和 `IRQENA`（流中断使能）两个寄存器——这就是为什么 `PsiMsDaq_Str_SetEnable` / `SetIrqEnable` 改的是 `STRENA`/`IRQENA` 而不是 `GCFG`。

  为什么要这样分层？因为复位期间（步骤 3）我们需要先关掉一切再清状态，如果 `ENA` 一直开着，清窗口计数时硬件可能正在往里写新数据，造成竞争。所以必须先 `GCFG=0` 关总使能 → 清干净 → 再把总使能打开，此后只在「流粒度」上控制采集。

#### 4.4.4 代码实践（源码阅读型 + 调用链跟踪）

**实践目标**：把本讲四节串起来——跟踪 `PsiMsDaq_Init` 里第一句 `PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_GCFG, 0)` 是怎么真正写到硬件的。

**操作步骤**：

1. 从 [psi_ms_daq.c:L156](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L156) 的 `PsiMsDaq_RegWrite(inst_p, PSI_MS_DAQ_REG_GCFG, 0)` 出发。
2. 跳到 4.2 节的间接层 [psi_ms_daq.c:L730-L740](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L730-L740)：`inst_p->regWrFct(inst_p->baseAddr + 0x000, 0)`。
3. 此时 `regWrFct` 在步骤 2 已被设成 `PsiMsDaq_RegWrite_Standard`（因为传了 `NULL`），跳到 4.1 节 [psi_ms_daq.c:L56-L60](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L56-L60)：`*(volatile uint32_t*)(baseAddr) = 0;`。
4. 这条解引用最终生成一条 `STR` 指令，经 AXI 互联写到 IP 的 `GCFG` 寄存器。

**需要观察的现象 / 预期结果**：你能画出三层调用栈——`Init` → `RegWrite`（间接层）→ `RegWrite_Standard`（裸指针）。每层职责单一：Init 决定「写什么」，间接层决定「往哪台 IP 写、用哪套访问函数」，Standard 决定「怎么发出这条访存指令」。

**待本地验证**：在带 SDK 的工程里给 `PsiMsDaq_RegWrite_Standard` 第一行下断点，运行 `PsiMsDaq_Init`，观察断点命中时 `addr` 参数是否等于传入的 `baseAddr`（因为 `GCFG` 偏移是 `0x000`）。

#### 4.4.5 小练习与答案

**练习 1**：如果把末尾的 `GCFG = ENA | IRQENA` 改成 `GCFG = 0`（保持禁能），驱动还能用吗？

**答案**：基本不能。`ENA=0` 时 IP 整体不工作，后续 `Str_Configure` 写进去的配置虽然能落到寄存器，但 `Str_SetEnable` 后硬件不会真正采集（因为全局使能没开）。`IRQENA=0` 则会导致即使某条流触发条件满足、`STRENA` 里对应位置了位，中断也发不出去。Init 必须置这两个全局位。

**练习 2**：为什么 Init 要在「禁能（步骤 3）」和「置全局使能（步骤 5）」之间插入「清窗口计数（步骤 4）」？

**答案**：为了在硬件「肯定没在写」的安全窗口里清掉残留状态。如果 `ENA` 开着、某条流可能正在采集，此时写 `WINCNT=0` 会和硬件的写操作竞争，可能清不掉或清错。先关总使能、清干净、再开总使能，是经典的「复位 → 配置 → 运行」三段式。

**练习 3**：`PsiMsDaq_Init` 返回的是句柄而不是返回码，如果失败（`malloc` 返回 `NULL`）调用方怎么知道？

**答案**：当前实现**没有**失败通道——`malloc` 失败会直接空指针解引用崩溃，调用方无从得知。这是「构造函数返回句柄」这一接口形状的代价；如果要做生产级加固，应在 `malloc` 后判 `NULL` 并返回 `NULL` 句柄，让调用方检查。本项目假定堆内存总是够用。

---

## 5. 综合实践

本讲的核心抽象是「**两层间接 + 可注入访问函数**」带来的跨平台可移植性。综合实践任务是把这条链路彻底走通，并亲手设计一个注入版访问函数。

### 任务一：解释默认实现为何能直接跑在 Zynq 裸机

请结合本讲 4.1、4.2 节，用你自己的话写一段说明，覆盖以下三点（写完后再对答案）：

1. **地址从哪来**：`PsiMsDaq_Init` 的 `baseAddr` 参数在 ZCU102 工程里通常来自哪里？
2. **`volatile` 的作用**：为什么 `PsiMsDaq_RegRead_Standard` 里那个指针必须加 `volatile`？
3. **为什么「直接解引用」在裸机可行**：裸机环境下 CPU 看到的地址空间有什么特点，使得「整数地址 → 指针 → 解引用」能直接命中 IP 寄存器？

**参考答案**：

1. 来自 Vitis/XSDK 根据 `component.xml` 自动生成的 `xparameters.h` 里的 `XPAR_PSI_MS_DAQ_AXI_BASEADDR`（u5-l3 会讲这条生成链路），它就是 Vivado 地址编辑器里给该 IP AXI Slave 分配的物理基址。
2. `volatile` 强制编译器对每次解引用都发出真实的 `LDR`/`STR` 指令，禁止把寄存器值缓存到 CPU 寄存器、禁止合并相邻访问。MMIO 寄存器的值会被硬件随时改变（如状态位、IRQVEC），没有 `volatile` 轮询状态位的循环会被优化死。
3. Zynq 裸机要么没有开 MMU（平坦地址空间），要么用了**恒等映射**（物理地址 `P` 映射到虚拟地址 `P`），所以 CPU 拿到的「整数地址」就等于 AXI 互联看到的物理地址，直接解引用即可命中外设。AXI 互联根据地址把读/写路由到挂在对应地址范围上的 IP AXI Slave。

### 任务二：为 Linux 用户态设计注入版访问函数

在 Linux 用户态，你不能直接解引用物理地址（MMU + 权限）。请按以下框架（**示例代码，非项目原有代码**）设计一个注入版读函数，填空并解释每一步：

```c
/* 示例代码：Linux 用户态注入版访问函数框架 */
#include <fcntl.h>
#include <sys/mman.h>
#include <stdint.h>

static int      mem_fd;                 /* /dev/mem 文件描述符 */
static void*    mapped_base;            /* mmap 返回的虚拟地址 */
static uint32_t ip_phys_base;           /* IP 物理基址 */
static uint32_t ip_map_size;            /* 映射区间大小 */

/* 1) 初始化阶段：打开 /dev/mem 并 mmap 一段覆盖 IP 寄存器区的虚拟地址 */
void linux_access_init(uint32_t phys_base, uint32_t size) {
    mem_fd = open("/dev/mem", O_RDWR | O_SYNC);
    /* TODO: 调用 mmap 把 [phys_base, phys_base+size) 映射进进程，
       结果存到 mapped_base；记录 ip_phys_base、ip_map_size */
}

/* 2) 注入版读寄存器：实现 PsiMsDaq_RegRead_f 签名 */
uint32_t linux_reg_read(const uint32_t addr) {
    /* addr 是“绝对物理地址” = phys_base + offset（由 RegRead 间接层算好）
       TODO: 把它换算成 mapped_base 里的偏移，再解引用返回 */
    /* 提示：offset = addr - ip_phys_base；指针 = mapped_base + offset */
    return /* TODO */;
}

/* 3) 把它注册进驱动 */
PsiMsDaq_AccessFct_t fcts = {
    .dataCopy = /* TODO: 同理需要把 DMA 物理地址换算进映射 */,
    .regWrite = linux_reg_write,
    .regRead  = linux_reg_read,
};
PsiMsDaq_IpHandle h = PsiMsDaq_Init(phys_base, streams, windows, &fcts);
```

**需要你回答**：

1. `linux_reg_read` 里为什么不能直接 `return *(uint32_t*)addr;`，而要先减 `ip_phys_base` 再加 `mapped_base`？
2. 为什么 `dataCopy` 也必须注入、不能继续用默认的 `memcpy`？（提示：DMA 写入数据的 `src` 地址来自 `StrConfig.bufStartAddr`，也是物理地址）
3. 在仿真环境（如 VHDL testbench + C 驱动）里，`linux_reg_read` 这一层应该换成什么？（提示：不是访存，而是调用仿真器的 poke/peek 后门）

**预期结果**（要点）：

1. 因为 `addr` 是物理地址，而 `mapped_base` 是 `mmap` 给进程的**虚拟**地址。`mmap` 返回的虚拟地址不一定等于物理地址（内核随便挑一段空闲虚拟空间），所以必须用 `mapped_base + (addr - ip_phys_base)` 把物理地址换算回映射区内的虚拟地址。
2. `GetDataUnwrapped` 里 `memcpyFct(buffer_p, (void*)(size_t)firstByteLinear, bytes)` 的 `src`（`firstByteLinear`）是 `bufStart + ...` 算出的**物理** DDR 地址。Linux 下 DMA 写入的物理 DDR 同样需要先 `mmap` 才能被 CPU 访问，所以 `dataCopy` 必须把物理 `src` 换算成映射后的虚拟地址再 `memcpy`。用默认 `memcpy` 会直接拿物理地址解引用 → 段错误。
3. 仿真环境里没有真实内存，注入版读写函数应该调用仿真器提供的后门 API（如 PSI 的 PsiSim/VProc 的 `VWrite`/`VRead`，或 Verilator 的 DPI），把「写寄存器」翻译成对 DUT 信号的一次驱动。这正是把访问层抽成可注入函数的最大收益——**同一份 `psi_ms_daq.c` 源码，裸机/Linux/仿真三种环境只换三个函数指针就能跑**。

### 任务三（进阶，可选）：手算 `strAddrOffs` 与窗口地址

给定 `maxWindows = 16`、`baseAddr = 0x80000000`：

1. 算 `strAddrOffs`（应为 `0x100`）。
2. 算流 2、窗口 5 的 `WINCNT`、`TSLO`、`TSHI` 三个寄存器的**绝对**地址（用 u3-l1 的宏 `PSI_MS_DAQ_WIN_WINCNT(n,w,so) = 0x4000 + so*n + 0x10*w` 等，再叠加 `baseAddr`）。

**预期结果**：

- `strAddrOffs = 0x100`。
- 偏移：`WINCNT = 0x4000 + 0x100*2 + 0x10*5 = 0x4250`；`TSLO = 0x4258`；`TSHI = 0x425C`。
- 绝对地址：`0x80004250` / `0x80004258` / `0x8000425C`。

**待本地验证**：在真实工程里用 Vivado 寄存器视图或 SDK 的 memory dump 工具读这些地址，看是否落在 IP AXI Slave 的地址范围内。

## 6. 本讲小结

- **三层调用栈**：高层 API → `PsiMsDaq_RegWrite/RegRead`（间接层：还原句柄、叠加 `baseAddr`、按函数指针分发）→ `PsiMsDaq_*_Standard`（最底层：`volatile` 裸指针解引用，生成 `LDR`/`STR`）。每层职责单一，是驱动可移植性的根基。
- **`volatile` 是 MMIO 的命门**：默认读写函数里那个 `volatile uint32_t*` 强制编译器每次都发出真实访存指令，没有它轮询状态位的循环会被优化死。
- **可注入访问函数（`PsiMsDaq_AccessFct_t`）是跨平台的关键**：传 `NULL` 走裸机直访；传自定义结构体走 Linux `mmap` 或仿真后门——同一份 `psi_ms_daq.c` 三处通用。
- **`strAddrOffs = Pow(2, Log2Ceil(maxWindows)) * 0x10`**：在 `maxWindows` 为 2 的幂时等于 `maxWindows*0x10`。`Log2Ceil` 实现其实是 floor，全靠「`maxWindows` 必须是 2 的幂」这一外部约束保证正确。
- **`PsiMsDaq_Init` 的复位序列是「禁能 → 清状态 → 置全局使能」三段式**：先 `GCFG=0/STRENA=0/IRQENA=0/IRQVEC=0xFFFFFFFF` 关一切并清残留中断，再逐流清 `MAXLVL` 与窗口计数并初始化软件字段，最后 `GCFG = ENA|IRQENA` 且此后不再动 `GCFG`。
- **全局使能与流级使能分层**：`GCFG.ENA`/`GCFG.IRQENA` 在 Init 末尾置一次后永不动；按流的采集开关、中断开关全部走 `STRENA`/`IRQENA`，避免与硬件采集竞争。

## 7. 下一步学习建议

本讲把「初始化」与「寄存器访问底座」讲完了，你现在能看懂驱动里任何一次寄存器读写的完整路径。接下来：

- **u3-l4 流配置流程**：紧接着看 `PsiMsDaq_Str_Configure`——它会大量使用本讲的 `RegWrite`、`RegSetField`、`RegSetBit`，以及守卫函数 `CheckStrDisabled`。学完你会看到「配置一个流」如何把 `StrConfig_t` 结构体逐字段写进 u3-l1 的那些寄存器。
- **u4-l4 寄存器字段/比特助手与返回码**：本讲只触及 `RegWrite`/`RegRead` 两个最基础的工具；`RegSetField`/`RegGetField`/`RegSetBit`/`RegGetBit` 这套「读改写（RMW）」工具、`SAFE_CALL` 短路宏、以及完整的 `PsiMsDaq_RetCode_t` 返回码集合，留到 u4-l4 集中讲。
- **想立刻看到真实用法**：跳到 `refdesign/ZCU102/Sdk/app/src/main.c` 的 `Init()` 函数，对照本讲的步骤 1~5，看参考应用是怎么调用 `PsiMsDaq_Init(..., NULL)` 走默认访问函数、再 `PsiMsDaq_GetStrHandle` 拿流句柄的（这条端到端链路会在 u5-l2 详细拆解）。
