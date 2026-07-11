# Windows WDF 驱动骨架与 INF

## 1. 本讲目标

本讲是专家层「设备驱动与软硬件接口」的第一讲，目标是从零理解 Mayoiuta 中那段把硬件 NPU 接入 Windows 操作系统的 C 代码。

读完本讲，你应当能够：

- 说清 Windows 内核驱动从被加载到收到第一个请求之间发生了什么，以及 `DriverEntry` 在其中的位置。
- 读懂 `NpuEvtDeviceAdd` 里「创建设备 → 注册中断 → 启用 DMA → 建立 IO 队列」这条装配流水线，并把每一步对上 `DEVICE_CONTEXT` 里的一个字段。
- 解释 `NPU_REGISTERS` 如何把一块物理内存（PCI BAR）映射成一组合名的 32/64 位寄存器。
- 说清 `setup.inf` 里 `PCI\VEN_1ACE&DEV_BEEF` 这串字符串与头文件里 `NPU_VENDOR_ID` / `NPU_DEVICE_ID` 宏的一一对应，以及 Windows 用它来「认出这块硬件该绑这个驱动」的机制。

本讲承接 u1-l3 的 `NPU_SOC`：那里我们看到了对外端口 `pcie_data` / `interrupt` / `status`，而这一讲讲的是**主机侧**——操作系统如何经 PCI 发现这块卡、加载驱动、再通过寄存器与 DMA 与之对话。

## 2. 前置知识

在进入源码前，先建立几个关键词的直觉。

**内核驱动（kernel driver）**。普通程序（用户态）崩了只挂自己；而运行在 CPU 特权级、直接碰硬件的代码是「内核驱动」，它崩了通常整台机器蓝屏（BSOD）。所以驱动代码格外强调「按框架规矩办事」。

**WDF（Windows Driver Framework）**。直接用 WDM（底层接口）写驱动繁琐且易错，微软提供了 WDF 这个面向对象风格的框架。WDF 的核心思想是：你不必手写设备栈、电源状态机的细节，只需**填回调**——「框架在某个时机要调用某个函数时，你想干啥」。本讲里所有名字形如 `NpuEvt...`（Evt = Event）的函数，都是这种「填给框架的回调」。

**PnP（即插即用）**。Windows 的 PnP 管理器负责枚举总线（这里是 PCI），发现设备后读取它的厂商/设备 ID，去匹配 `.inf` 文件，匹配上就把对应驱动加载起来，并依次调用驱动的回调来「加电、映射资源、启动」。`EvtDeviceAdd` 就是 PnP 流程的关键一环。

**MMIO（Memory-Mapped I/O）**。CPU 与外设交换数据最常见的方式之一：把一段物理地址「借给」设备，CPU 对这段地址的读写会变成对设备寄存器的访问。`MmMapIoSpace` 就是把物理地址翻成内核可访问的虚拟地址指针的工具。

**DMA（Direct Memory Access）**。大批量数据（如 NPU 的权重）若逐字节经 CPU 搬运会很慢；DMA 让设备**直接**读写主机内存。散列收集（Scatter-Gather）DMA 是其中一种模式，能处理物理上不连续的内存。

> 术语速查：**BAR**（Base Address Register，PCI 设备声明的地址窗口）、**ISR**（Interrupt Service Routine，中断服务例程，跑在高 IRQL）、**DPC**（Deferred Procedure Call，延迟过程调用，把不那么急的活儿挪到低 IRQL 做）、**IRQL**（中断请求级，决定能被什么打断）、**IOCTL**（I/O Control，用户态向驱动下发自定义命令的码）。

## 3. 本讲源码地图

本讲涉及三个文件，全部位于 `driver/win32/`，是 Mayoiuta 的 Windows 内核驱动三件套：

| 文件 | 作用 | 本讲侧重 |
| --- | --- | --- |
| [driver/win32/npudriver.h](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h) | 头文件：设备/厂商 ID、IOCTL 码、`NPU_REGISTERS` 寄存器布局、`DEVICE_CONTEXT` 上下文结构 | 4.3 节核心 |
| [driver/win32/npudriver.c](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c) | 驱动实现：`DriverEntry`、`NpuEvtDeviceAdd`、`NpuEvtPrepareHardware` 等回调 | 4.1 / 4.2 / 4.3 节 |
| [driver/win32/setup.inf](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf) | 安装清单：声明厂商、PCI 匹配串、服务安装方式 | 4.4 节核心 |

提醒：本仓库**不包含**任何构建脚本（无 `.vcxproj` / `Makefile` / WDK 配置），也无数字签名目录文件（`.cat`）。因此这份驱动能否原样编译、上机加载，**待本地/上机验证**。本讲把它当作「教学骨架」逐行精读，并对其中可疑写法标注「待确认」。

---

## 4. 核心概念与源码讲解

### 4.1 驱动入口 DriverEntry

#### 4.1.1 概念说明

`DriverEntry` 是内核驱动的 `main`——驱动被服务控制管理器加载进内核时，第一个被调用的函数就是它。它的职责很克制：**注册自己**。

在 WDF 模型里，`DriverEntry` 通常只做一件事：调用 `WdfDriverCreate` 创建一个「框架驱动对象」，并把一个回调 `EvtDeviceAdd` 交给框架——意思是「以后 PnP 管理器每发现一个我负责的设备，就调用这个函数」。注意它**不**在这里创建设备、不在这里碰硬件：那些是设备级的事，要等 `EvtDeviceAdd`。

#### 4.1.2 核心流程

`DriverEntry` 的执行可以概括为三步：

1. 用 `WDF_DRIVER_CONFIG_INIT` 初始化一份配置，把 `NpuEvtDeviceAdd` 登记为「设备加入」回调。
2. 顺带设置一个内存池标签（pool tag），供调试时识别「这块内存是 NPU 驱动分配的」。
3. 调用 `WdfDriverCreate` 让框架创建驱动对象；成功返回状态码，失败则打印并返回（框架会卸载驱动）。

时序上，一次完整的「装上 NPU 卡 → 跑起来」是这样衔接的：

```
Windows 启动/插卡
   │  PnP 管理器枚举 PCI，读到 VEN_1ACE&DEV_BEEF（见 4.4）
   ▼
按 setup.inf 找到 npudriver.sys 并加载
   │  调用 DriverEntry  ← 本节
   ▼
PnP 管理器发现一个匹配设备
   │  调用 NpuEvtDeviceAdd  ← 4.2 节
   ▼
资源就绪
   │  调用 NpuEvtPrepareHardware（映射寄存器、复位 NPU）  ← 4.3 节
   ▼
设备启动，等待应用层 IOCTL  ← u5-l2 讲
```

#### 4.1.3 源码精读

文件开头先做了一组前向声明（[driver/win32/npudriver.c:3-8](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L3-L8)），把本驱动用到的回调名字「登记」一下。其中 `EVT_WDF_DRIVER_DEVICE_ADD NpuEvtDeviceAdd;` 是 WDF 惯用写法：`EVT_WDF_DRIVER_DEVICE_ADD` 是框架定义的一个**函数类型**，用它声明 `NpuEvtDeviceAdd`，等价于一次性声明了完整签名，省得手写参数列表。

`DriverEntry` 本体（[driver/win32/npudriver.c:10-22](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L10-L22)）：

```c
WDF_DRIVER_CONFIG config;
WDF_DRIVER_CONFIG_INIT(&config, NpuEvtDeviceAdd);   // 登记 EvtDeviceAdd
config.DriverPoolTag = 'UPN';                        // 池标签
status = WdfDriverCreate(DriverObject, RegistryPath,
            WDF_NO_OBJECT_ATTRIBUTES, &config, WDF_NO_HANDLE);
```

要点逐句拆：

- `WDF_DRIVER_CONFIG_INIT(&config, NpuEvtDeviceAdd)`：把 `NpuEvtDeviceAdd` 这个回调地址写进配置结构。从此框架知道「有设备加入时调谁」。
- `config.DriverPoolTag = 'UPN';`：池标签是一个 4 字节常量，调试器/`!poolused` 用它识别内存来源。这里写的是 3 字符 `'UPN'`——多字符常量（multi-char constant）在 C 中是**实现定义**的，且约定池标签一般凑足 4 字符，所以这里严格说是「待确认/不规范写法」，建议写成 `'NPU0'` 之类。
- `WdfDriverCreate(...)`：真正建对象。第 5 个参数传 `WDF_NO_HANDLE` 表示不需要拿到驱动对象的句柄（本驱动后面也没再用到它）。
- 失败时用 `KdPrint` 打印（仅在 checked build 生效），并把 `status` 返回给框架——失败则框架不会继续加载。

#### 4.1.4 代码实践

**实践目标**：理解 `DriverEntry`「只注册、不干活」的角色。

**操作步骤**：

1. 打开 [driver/win32/npudriver.c:10-22](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L10-L22)。
2. 确认其中**没有**任何对硬件寄存器、中断、DMA 的访问——这些都被推迟到了后续回调。
3. 把 `config.DriverPoolTag` 改成 `'NPU0'`（4 字符），并思考：为什么调试时这个标签有用？

**需要观察的现象**：纯源码阅读型实践，无需运行。

**预期结果**：你会确认 `DriverEntry` 唯一的「副作用」是创建框架驱动对象并登记 `EvtDeviceAdd`，整机行为不在此处发生变化。本仓库无构建系统，是否可编译**待本地验证**。

#### 4.1.5 小练习与答案

1. **问**：如果 `WdfDriverCreate` 失败，`DriverEntry` 直接 `return status`，会发生什么？
   **答**：返回失败状态码后，框架/系统认为驱动初始化失败，该驱动不会被保留在内核中，后续 `EvtDeviceAdd` 也不会被调用。

2. **问**：为什么 `DriverEntry` 里**不**直接创建设备对象？
   **答**：因为一个驱动可能管多个同类设备，设备对象的创建属于「每个设备一次」的事，由 PnP 流程在 `EvtDeviceAdd` 中按需触发；`DriverEntry` 是「整张驱动一次」的初始化。

---

### 4.2 设备创建 NpuEvtDeviceAdd

#### 4.2.1 概念说明

`NpuEvtDeviceAdd` 是 `DriverEntry` 登记给框架的回调，名字里的 `Evt`（Event）+ `DeviceAdd` 已经点明了它的触发时机：**PnP 管理器发现一个由本驱动负责的设备时调用**。可以把它理解成「设备对象的总装车间」——它把一台抽象的 WDF 设备从无到有地装配起来，并把这台设备专属的运行时信息（中断、DMA、队列、寄存器指针）挂到一个叫 `DEVICE_CONTEXT` 的结构上。

#### 4.2.2 核心流程

`NpuEvtDeviceAdd` 内部是一条有序的装配流水线，每一步都对应 `DEVICE_CONTEXT` 的一个字段：

```
1. 注册 PnP/电源回调（EvtDevicePrepareHardware）          →  无字段（写进框架）
2. 创建设备对象，绑定 DEVICE_CONTEXT                      →  devContext->Device
3. 创建中断对象（登记 ISR + DPC）                          →  devContext->Interrupt
4. 创建 DMA Enabler（散列收集 64 位）                      →  devContext->DmaEnabler
5. 创建默认 IO 队列（Parallel 派发，登记 IOCTL 回调）       →  devContext->IoQueue
```

装配完成后，设备进入「资源就绪」阶段，框架随后调用 `NpuEvtPrepareHardware`（4.3 节）做寄存器映射与复位。

#### 4.2.3 源码精读

函数签名（[driver/win32/npudriver.c:24-27](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L24-L27)）的第一个参数是 `WDFDRIVER Driver`、第二个是 `PWDFDEVICE_INIT DeviceInit`——这正是 WDF `EVT_WDF_DRIVER_DEVICE_ADD` 回调的标准形态：`DeviceInit` 是框架给的「半成品设备初始化结构」，你在创建设备前可以往里塞各种配置。

**第一步：登记 PnP/电源回调**（[driver/win32/npudriver.c:33-35](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L33-L35)）：

```c
WDF_PNPPOWER_EVENT_CALLBACKS_INIT(&pnpCallbacks);
pnpCallbacks.EvtDevicePrepareHardware = NpuEvtPrepareHardware;
WdfDeviceInitSetPnpPowerEventCallbacks(DeviceInit, &pnpCallbacks);
```

把 `NpuEvtPrepareHardware` 登记成「硬件就绪」回调，并塞进 `DeviceInit`。

**第二步：创建设备对象并取出上下文**（[driver/win32/npudriver.c:37-44](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L37-L44)）：

```c
WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&attributes, DEVICE_CONTEXT);
status = WdfDeviceCreate(&DeviceInit, &attributes, &device);
devContext = GetDeviceContext(device);
devContext->Device = device;
```

`WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE` 告诉框架「每个设备对象自带一块 `DEVICE_CONTEXT` 大小的空间」。建好后 `GetDeviceContext(device)` 取出这块空间的指针，后续中断/DMA/队列的句柄都存进去。

**第三步：创建中断对象**（[driver/win32/npudriver.c:46-52](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L46-L52)）：

```c
WDF_INTERRUPT_CONFIG_INIT(&interruptConfig, NpuEvtInterruptIsr, NpuEvtInterruptDpc);
interruptConfig.InterruptTranslated = WdfUseHardwareInterrupts;
status = WdfInterruptCreate(device, &interruptConfig, ..., &devContext->Interrupt);
```

把 ISR（`NpuEvtInterruptIsr`）和 DPC（`NpuEvtInterruptDpc`）成对登记：ISR 在硬件中断到来时立刻跑（高 IRQL，只能做最急的事——确认中断、排队 DPC），DPC 随后在低 IRQL 跑「不那么急但更耗时」的善后。这里出现的 `WdfUseHardwareInterrupts` 是非常规写法，**待确认**其是否为有效常量；标准 WDF 代码通常让框架从 PnP 资源中自动解析中断。

**第四步：创建 DMA Enabler**（[driver/win32/npudriver.c:54-60](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L54-L60)）：

```c
WDF_DMA_ENABLER_CONFIG_INIT(&dmaConfig, WdfDmaProfileScatterGather64, 64);
status = WdfDmaEnablerCreate(device, &dmaConfig, ..., &devContext->DmaEnabler);
```

声明本设备用「散列收集、64 位地址」DMA，最大地址 64 位。`DmaEnabler` 是后续发起 DMA 事务（u5-l2 详讲）的「总开关」。

**第五步：创建默认 IO 队列**（[driver/win32/npudriver.c:62-68](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L62-L68)）：

```c
WDF_IO_QUEUE_CONFIG_INIT_DEFAULT_QUEUE(&ioQueueConfig, WdfIoQueueDispatchParallel);
ioQueueConfig.EvtIoDeviceControl = NpuEvtIoDeviceControl;
status = WdfIoQueueCreate(device, &ioQueueConfig, ..., &devContext->IoQueue);
```

「默认队列」接收所有来到本设备的请求；`WdfIoQueueDispatchParallel` 表示请求可以**并行**派发（不排队串行），由 `NpuEvtIoDeviceControl` 处理 IOCTL。

**一处待确认的隐患**：`DEVICE_CONTEXT` 里有 `WDFSPINLOCK DmaLock` 字段（见 4.3 节头文件），且 `NpuEvtIoDeviceControl` 里 `WdfSpinLockAcquire(devContext->DmaLock)` 使用了它，但翻遍 `NpuEvtDeviceAdd` **没有任何 `WdfSpinLockCreate`** 来初始化它。也就是说这个自旋锁句柄从未被赋值就在 IOCTL 路径里被获取——这是一个潜在 bug。修复方式是在本函数里补一句 `WdfSpinLockCreate(...)` 给 `devContext->DmaLock` 赋值（具体写法**待本地/上机验证**）。

#### 4.2.4 代码实践

**实践目标**：把 `NpuEvtDeviceAdd` 的五步装配与 `DEVICE_CONTEXT` 的字段一一对应起来。

**操作步骤**：

1. 打开 [driver/win32/npudriver.c:24-71](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L24-L71)。
2. 同时打开 [driver/win32/npudriver.h:28-35](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L28-L35) 中的 `DEVICE_CONTEXT`。
3. 做一张表：左列是 `DEVICE_CONTEXT` 每个字段，右列是「在哪一行被赋值」。
4. 找出 `DEVICE_CONTEXT` 中**从未被本驱动赋值**的字段（提示：`DmaLock`），记下它后来在何处被使用（`NpuEvtIoDeviceControl`）。

**需要观察的现象**：纯阅读型实践。

**预期结果**：你会得到一张「字段 ↔ 赋值点」对照表，并发现 `DmaLock` 是缺口。本仓库无构建系统，运行期行为**待上机验证**。

#### 4.2.5 小练习与答案

1. **问**：为什么中断要登记 **ISR + DPC** 两个回调，而不是只留一个？
   **答**：ISR 在高 IRQL 跑，整机很多活动被屏蔽，必须尽快返回；它只做最急的事（确认中断、请求一个 DPC）。耗时的善后放到 DPC 里在低 IRQL 完成，既不漏响应也不拖累系统。

2. **问**：`WdfIoQueueDispatchParallel` 与「串行派发」相比，对本 NPU 驱动意味着什么？
   **答**：并行派发允许同时把多个 IOCTL 交给驱动并发处理；对 NPU 这种可能并发收到多个执行/加载请求的设备能提高吞吐，代价是驱动自己要处理并发（正因此才需要 `DmaLock` 这类自旋锁）。

3. **问**：`WDF_OBJECT_ATTRIBUTES_INIT_CONTEXT_TYPE(&attributes, DEVICE_CONTEXT)` 这一行如果删掉，会怎样？
   **答**：框架不会为设备对象预留 `DEVICE_CONTEXT` 空间，`GetDeviceContext(device)` 取到的指针无效，后续访问 `devContext->...` 会出问题。这一行是把「每设备私有数据」接到框架对象上的关键。

---

### 4.3 寄存器映射 NPU_REGISTERS 与 DEVICE_CONTEXT

#### 4.3.1 概念说明

前两节解决了「框架怎么把驱动挂起来」「设备对象怎么装配」。本节解决一个更贴近硬件的问题：**CPU 怎么用 C 语言的指针去读写 NPU 上的寄存器？**

答案分两部分：

- **`NPU_REGISTERS`**：一个 `struct`，把「这块 NPU 暴露的一组寄存器」按固定顺序列出来。一旦把这块寄存器区域的起始地址强转成 `NPU_REGISTERS*`，那么 `regs->ControlStatus` 就直接对应到偏移 0 处的那个 32 位寄存器——访问硬件变成了访问结构体成员。
- **`DEVICE_CONTEXT`**：每个设备对象的「随身口袋」，里面装着这台设备运行时所有要记住的东西，其中就包括上面那个 `NPU_REGISTERS* Registers` 指针，外加中断、DMA、队列、自旋锁的句柄。

把寄存器物理地址变成 `Registers` 指针的工作，发生在 `NpuEvtPrepareHardware` 里。

#### 4.3.2 核心流程

```
NpuEvtPrepareHardware 被框架调用（资源已就绪）
   │  遍历框架给的「翻译后资源列表」
   ▼
找到 CmResourceTypeMemory（一段物理地址 = PCI BAR）
   │  MmMapIoSpaceEx(物理地址, 长度, PAGE_READWRITE|PAGE_NOCACHE)
   ▼
得到内核虚拟地址，强转为 NPU_REGISTERS* 存进 devContext->Registers
   │  随后写 ControlStatus=1（复位）→ 停 100 微秒 → 写 0（解除复位）
   ▼
此后全驱动经 devContext->Registers->XXX 访问硬件
```

`MmMapIoSpace` 之所以指定 `PAGE_NOCACHE`（不缓存），是因为寄存器读写必须直达硬件——若被 CPU 缓存，连续写两次「复位/解除复位」可能合并而失效。

#### 4.3.3 源码精读

**头文件中的两个结构**。先看 [driver/win32/npudriver.h:18-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L18-L25) 的寄存器布局：

```c
typedef struct _NPU_REGISTERS {
    ULONG   ControlStatus;   // 控制/状态：0 复位/1 解除/2 启动计算；bit31 中断标志
    ULONG   InterruptMask;   // 中断屏蔽/确认
    ULONG64 DmaSource;       // DMA 源地址（主机内存）
    ULONG64 DmaDest;         // DMA 目的地址（设备侧）
    ULONG   DmaLength;       // DMA 长度
} NPU_REGISTERS, *PNPU_REGISTERS;
```

把 NPU 的对外寄存器归纳成 5 个：一个全能的 `ControlStatus`（既作控制也作状态），一个 `InterruptMask`，三个用于 DMA（`DmaSource`/`DmaDest`/`DmaLength`）。注意 `ControlStatus` 的不同位/取值在驱动里被赋予了不同含义（见下面 PrepareHardware 与 ISR 的赋值），这本质上是硬件与驱动之间的一个约定，**该约定是否与 RTL 侧 `NPU_SOC` 的端口语义一致，待对照确认**（仓库未提供两侧接口文档）。

再看 [driver/win32/npudriver.h:28-37](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L28-L37) 的设备上下文：

```c
typedef struct _DEVICE_CONTEXT {
    WDFDEVICE      Device;      // 自身设备对象
    NPU_REGISTERS* Registers;   // 映射后的寄存器指针
    WDFDMAENABLER  DmaEnabler;  // DMA 总开关
    WDFINTERRUPT   Interrupt;   // 中断对象
    WDFSPINLOCK    DmaLock;     // IOCTL 用的自旋锁（见 4.2 隐患）
    WDFQUEUE       IoQueue;     // IO 队列
} DEVICE_CONTEXT, *PDEVICE_CONTEXT;

WDF_DECLARE_CONTEXT_TYPE_WITH_NAME(DEVICE_CONTEXT, GetDeviceContext)
```

最后一行 `WDF_DECLARE_CONTEXT_TYPE_WITH_NAME` 是个宏，它生成了我们在 4.2 节反复调用的 `GetDeviceContext(device)` 访问器——这正是「框架对象 + 私有上下文」机制的粘合剂。

**PrepareHardware 如何填上 `Registers`**（[driver/win32/npudriver.c:73-101](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L73-L101)）：

```c
for (ULONG i = 0; i < resourceCount; i++) {
    resource = WdfCmResourceListGetDescriptor(ResourcesTranslated, i);
    if (resource->Type == CmResourceTypeMemory) {
        devContext->Registers = (NPU_REGISTERS*)MmMapIoSpaceEx(
            resource->u.Memory.Start,
            resource->u.Memory.Length,
            PAGE_READWRITE | PAGE_NOCACHE);
        if (!devContext->Registers) return STATUS_INSUFFICIENT_RESOURCES;
    }
}
// 复位 NPU
WRITE_REGISTER_ULONG(&devContext->Registers->ControlStatus, 0x1); // 复位
KeStallExecutionProcessor(100);                                    // 忙等 100us
WRITE_REGISTER_ULONG(&devContext->Registers->ControlStatus, 0x0); // 解除复位
```

要点：

- 遍历框架给的资源列表，挑出 `CmResourceTypeMemory`（一段内存窗口，通常就是 PCI BAR）。
- 用 `MmMapIoSpaceEx` 把物理地址映射成内核虚拟地址，强转为 `NPU_REGISTERS*` 存进上下文。
- 之后用 `WRITE_REGISTER_ULONG` 写 `ControlStatus`：先 `0x1` 复位、忙等 100 微秒、再 `0x0` 解除复位——经典的「复位 → 等稳定 → 解除复位」上电序列。
- 注意此循环只处理 `CmResourceTypeMemory`，并不处理中断资源（中断由 `WdfInterruptCreate` + 框架自动解析）。

**ISR 如何用这同一个指针读状态**（[driver/win32/npudriver.c:179-189](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L179-L189)）：

```c
ULONG status = READ_REGISTER_ULONG(&devContext->Registers->ControlStatus);
if (status & 0x80000000) {                       // bit31 为中断标志
    WRITE_REGISTER_ULONG(&devContext->Registers->InterruptMask, 0x1); // 确认
    return TRUE;                                 // 声明中断由本驱动处理
}
return FALSE;
```

这印证了 4.3.1 的设计：硬件访问 = 结构体成员访问。读 `ControlStatus` 看 bit31 判断是不是本设备来的中断，是则写 `InterruptMask` 确认（acknowledge）并返回 `TRUE`。

#### 4.3.4 代码实践

**实践目标**：亲手算出 `NPU_REGISTERS` 每个字段的字节偏移，理解「结构体成员 = 硬件寄存器」。

**操作步骤**：

1. 打开 [driver/win32/npudriver.h:19-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L19-L25)。
2. 假设 `ULONG` = 4 字节、`ULONG64` = 8 字节、默认 8 字节对齐，按下表填写：

   | 字段 | 大小 | 起始偏移 | 结束偏移（不含） |
   | --- | --- | --- | --- |
   | ControlStatus | 4 | ? | ? |
   | InterruptMask | 4 | ? | ? |
   | DmaSource | 8 | ? | ?（注意对齐） |
   | DmaDest | 8 | ? | ? |
   | DmaLength | 4 | ? | ? |

3. 对照 [driver/win32/npudriver.c:84-87](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L84-L87)，确认映射长度取自资源 `Memory.Length`，思考：若硬件 BAR 实际长度小于上述结构体总长，访问尾部字段会发生什么？

**需要观察的现象**：纯计算 + 阅读型实践。

**预期结果**：你会得到各字段偏移（`DmaSource` 因 8 字节对齐会在偏移 8 起步，前面有 4 字节 `ControlStatus`+4 字节 `InterruptMask` 正好凑齐），并理解结构体布局必须与硬件寄存器手册严格一致，否则会读写到错误的寄存器。具体偏移与硬件一致性**待对照 RTL 确认**。

#### 4.3.5 小练习与答案

1. **问**：为什么 `MmMapIoSpaceEx` 必须加 `PAGE_NOCACHE`？
   **答**：寄存器是设备侧状态，每次读写都必须直达硬件；若被 CPU 缓存，复位/解除复位这类「靠连续两次写产生边沿」的操作可能被合并或延迟，行为不正确。

2. **问**：`NPU_REGISTERS` 结构体的字段顺序能不能随便调？
   **答**：不能。字段顺序决定了每个成员对应的字节偏移，而偏移必须与硬件寄存器在 BAR 中的物理排布一一对应。调换顺序就等于读写到错误的寄存器。

3. **问**：`WRITE_REGISTER_ULONG` 与直接 `devContext->Registers->ControlStatus = 0x1` 相比，为什么用前者？
   **答**：`WRITE_REGISTER_ULONG` 是为 MMIO 设计的宏，保证编译器不会把写操作优化掉、合并或重排，确保每次写都按预期落到硬件；直接赋值在开启优化时可能被合并或消除。

---

### 4.4 INF 与 PCI 设备匹配

#### 4.4.1 概念说明

驱动代码（`.c`/`.h`）写得再好，Windows 也不会凭空知道「这张 NPU 卡该用 `npudriver.sys`」。牵线搭桥的是 `setup.inf`——一份纯文本的安装清单。它的核心使命是回答一个问题：**「这张硬件的标识，和我这个驱动，对得上吗？」**

硬件标识来自 PCI 总线：每张 PCI 设备都暴露一个**厂商 ID（Vendor ID，VEN）**和一个**设备 ID（Device ID，DEV）**。PnP 管理器枚举到设备后会读出这两个 ID，把它们拼成 `PCI\VEN_xxxx&DEV_yyyy` 这样的字符串，再去所有 INF 里找匹配项。匹配上，就把该 INF 指定的驱动服务加载起来。

这就是为什么头文件里的 `NPU_VENDOR_ID` / `NPU_DEVICE_ID` 必须与 INF 里的 `VEN_` / `DEV_` 字符串**完全一致**——它们是同一条事实的两个表达。

#### 4.4.2 核心流程

```
上电/插卡 → PCI 总线枚举
   │  读出设备 VEN=0x1ACE、DEV=0xBEEF
   ▼
PnP 管理器构造硬件 ID 字符串 PCI\VEN_1ACE&DEV_BEEF
   │  扫描系统 INF，匹配 setup.inf 的 [DeviceList.NTamd64]
   ▼
命中 → 按 [DeviceInstall.Services] 加载 npudriver 服务
   │  ServiceBinary=%12%\npudriver.sys
   ▼
驱动入内核 → DriverEntry（回到 4.1）
```

#### 4.4.3 源码精读

**头文件里的标识**（[driver/win32/npudriver.h:6-7](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L6-L7)）：

```c
#define NPU_DEVICE_ID  0xBEEF
#define NPU_VENDOR_ID  0x1ACE
```

这两个宏目前**只在头文件里定义、源码里并未被引用**（驱动运行时并不需要自报家门）。它们的真正价值是作为**文档**：把「这个驱动认领的硬件 ID」写进源码，让人一眼对上 INF。

**INF 的版本与厂商段**（[driver/win32/setup.inf:2-11](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf#L2-L11)）声明这是 `System` 类驱动、厂商字符串占位符 `%Manufacturer%`，并把设备列表指向 `DeviceList,NTamd64`。

**关键的匹配行**（[driver/win32/setup.inf:13-14](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf#L13-L14)）：

```inf
[DeviceList.NTamd64]
%DeviceDesc%=DeviceInstall, PCI\VEN_1ACE&DEV_BEEF
```

这一行就是「红线」：左边的 `%DeviceDesc%`（在 `[Strings]` 里展开为 "Neural Processing Unit"）是设备友好名；中间 `DeviceInstall` 指向下面要用的安装节；右边 `PCI\VEN_1ACE&DEV_BEEF` 就是与硬件直接对标的 ID 串——`1ACE` 对应 `NPU_VENDOR_ID`、`BEEF` 对应 `NPU_DEVICE_ID`。注意 ID 是**十六进制**，且字母大小写需与总线报告一致。

**安装节引用了微软的 HDC INF**（[driver/win32/setup.inf:16-18](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf#L16-L18)）：

```inf
[DeviceInstall]
Include=mshdc.inf
Needs=MSHDC.NT
```

`mshdc.inf` 是微软 IDE/ATA 控制器的系统 INF。NPU 驱动 `Include`/`Needs` 它，**待确认**是否有意为之——很可能是模板复制留下的痕迹，对一个 AI 加速设备来说并不恰当；真实场景这里通常应直接声明自己的服务需求。

**服务安装段**（[driver/win32/setup.inf:20-28](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf#L20-L28)）：

```inf
[DeviceInstall.Services]
AddService=npudriver,0x00000002,ServiceInstall

[ServiceInstall]
DisplayName=%ServiceName%
ServiceType=1            ; SERVICE_KERNEL_DRIVER
StartType=3              ; SERVICE_DEMAND_START（按需/手动启动）
ErrorControl=1
ServiceBinary=%12%\npudriver.sys   ; %12% = %SystemRoot%\System32\drivers
```

要点：`AddService` 名字为 `npudriver`，标志 `0x00000002`（SPSVCINST_ASSOCSERVICE）表示这是该设备的「主服务」；`StartType=3` 表示**手动启动**（PnP 触发时才加载，而非开机即加载）；`%12%` 是 dirid，指向 `System32\drivers` 目录，所以编译产物 `npudriver.sys` 要落到那里。

**版本信息与签名占位**（[driver/win32/setup.inf:7-8](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf#L7-L8)）：`DriverVer=29/01/2025,1.0.0.0` 记录驱动版本日期；`CatalogFile=npudriver.cat` 指向数字签名目录文件，但**仓库未提供该 `.cat`**，故实际能否在开了签名校验的系统上安装，**待确认**。

#### 4.4.4 代码实践（本讲指定实践任务）

**实践目标**：把 INF 的 PCI 匹配串与头文件的宏对应起来，验证「同一事实两处表达」。

**操作步骤**：

1. 读 [driver/win32/setup.inf:14](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/setup.inf#L14) 中的 `PCI\VEN_1ACE&DEV_BEEF`。
2. 读 [driver/win32/npudriver.h:6-7](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L6-L7) 中的 `NPU_DEVICE_ID 0xBEEF` 与 `NPU_VENDOR_ID 0x1ACE`。
3. 把十六进制 `0x1ACE` / `0xBEEF` 与 INF 字符串里的 `1ACE` / `BEEF` 一一比对。
4. 回答：该驱动绑定的**厂商 ID** 与**设备 ID** 分别是什么？`NPU_VENDOR_ID`/`NPU_DEVICE_ID` 宏与 INF 字符串的对应关系是怎样的？

**需要观察的现象**：纯阅读比对型实践。

**预期结果**：

- 厂商 ID = `0x1ACE`（INF 写作 `VEN_1ACE`），设备 ID = `0xBEEF`（INF 写作 `DEV_BEEF`）。
- 对应关系：`NPU_VENDOR_ID (0x1ACE)` ↔ INF `VEN_1ACE`；`NPU_DEVICE_ID (0xBEEF)` ↔ INF `DEV_BEEF`。头文件宏以 C 十六进制字面量表达，INF 以 PCI ID 字符串表达，二者必须一致才能完成 PnP 匹配。
- 进阶发现：头文件里这两个宏**未被任何 C 代码引用**，目前仅起「文档对齐」作用；可考虑在 INF 生成脚本里引用它们以避免两处不一致（属改进建议，**待实施验证**）。

#### 4.4.5 小练习与答案

1. **问**：若 RTL 侧把设备 ID 从 `0xBEEF` 改成了 `0xCAFE`，但忘记改 INF，会发生什么？
   **答**：PnP 枚举出的字符串变成 `PCI\VEN_1ACE&DEV_CAFE`，与 INF 里的 `DEV_BEEF` 不匹配，驱动不会被加载——卡变成「无驱动设备」。

2. **问**：`StartType=3`（手动启动）与 `StartType=0`（开机启动）对 NPU 驱动各有什么影响？
   **答**：NPU 是 PCI 设备，由 PnP 在设备出现时按需加载，`StartType=3` 正契合这种「插卡/枚举触发」的模型；`StartType=0` 会在开机时硬加载，通常用于必须早早运行的系统驱动，对 PCI PnP 设备并不合适。

3. **问**：`%12%` 这个目录占位符最终指向哪里？为什么 `npudriver.sys` 要放那儿？
   **答**：`%12%` 即 dirid 12，展开为 `%SystemRoot%\System32\drivers`，这是 Windows 加载内核驱动的标准目录，`ServiceBinary` 必须指向这里系统才能找到驱动映像。

---

## 5. 综合实践

**任务**：绘制一张「从插卡到第一个 IOCTL」的完整时序图，把本讲四个模块（`DriverEntry`、`NpuEvtDeviceAdd`、`DEVICE_CONTEXT`、`NPU_REGISTERS`）串起来，并标出全部「待确认」点。

**操作步骤**：

1. 在纸上画出以下参与方：**PCI 总线 / PnP 管理器**、**setup.inf**、**npudriver.sys**、**框架 WDF**、**NPU 硬件寄存器**、**应用层进程**。
2. 按时间从上到下画出事件流，每条事件标注触发它的模块与本讲对应小节：
   - PnP 枚举 → 读出 `VEN_1ACE&DEV_BEEF`（4.4）
   - 匹配 INF → 加载 `npudriver.sys` → 调 `DriverEntry`（4.1）
   - 发现设备 → 调 `NpuEvtDeviceAdd` → 依次创建设备/中断/DMA/IO 队列（4.2）
   - 资源就绪 → 调 `NpuEvtPrepareHardware` → `MmMapIoSpaceEx` 得到 `Registers` 指针、复位 NPU（4.3）
   - 应用层 `DeviceIoControl` → 进 IO 队列 → `NpuEvtIoDeviceControl`（预告 u5-l2）
3. 在图上用**红笔**标出本讲发现的所有「待确认/隐患」点，至少包括：
   - `DmaLock` 被使用却从未创建（4.2）
   - 池标签 `'UPN'` 非 4 字符（4.1）
   - `WdfUseHardwareInterrupts` 取值待确认（4.2）
   - `NPU_REGISTERS` 字段语义与 RTL 侧 `NPU_SOC` 端口的一致性待对照（4.3）
   - INF `Include=mshdc.inf` 是否恰当、`.cat` 签名缺失、本仓库无构建系统（4.4）
4. 写一段 200 字以内的结论：这份驱动目前处于什么完成度？

**预期结果**：一张能解释「硬件如何被认领、驱动如何被挂起、寄存器如何被映射」的端到端图，并清晰区分「已实现」与「待确认」。结论应点出：这是一份结构完整、装配流水线清晰，但**未经验证编译**且含若干 API/资源缺口的教学骨架。

## 6. 本讲小结

- `DriverEntry` 是内核驱动的入口，职责克制——只调 `WdfDriverCreate` 创建框架驱动对象并登记 `EvtDeviceAdd`，不碰硬件。
- `NpuEvtDeviceAdd` 是「设备总装车间」，按「设备对象 → 中断 → DMA Enabler → IO 队列」五步装配，每步的句柄存进 `DEVICE_CONTEXT` 对应字段。
- `DEVICE_CONTEXT` 是每设备的「随身口袋」（含 `Registers`/`Interrupt`/`DmaEnabler`/`IoQueue`/`DmaLock`），靠 `WDF_DECLARE_CONTEXT_TYPE_WITH_NAME` 生成的 `GetDeviceContext` 访问。
- `NPU_REGISTERS` 把 PCI BAR 这块内存窗口按固定偏移命名成一组寄存器；`NpuEvtPrepareHardware` 用 `MmMapIoSpaceEx` 把物理地址映射成 `NPU_REGISTERS*`，此后硬件访问即结构体成员访问。
- `setup.inf` 里 `PCI\VEN_1ACE&DEV_BEEF` 是设备匹配的红线，与头文件 `NPU_VENDOR_ID=0x1ACE` / `NPU_DEVICE_ID=0xBEEF` 是同一事实的两处表达，必须一致。
- 本驱动是结构完整但**未经编译验证**的教学骨架：`DmaLock` 未创建即被使用、`Include=mshdc.inf` 可疑、`.cat` 与构建系统缺失等多项**待确认**。

## 7. 下一步学习建议

下一讲 **u5-l2 驱动 IOCTL、DMA 与中断交互** 将继续精读 `npudriver.c`，重点放在本讲只略提的 `NpuEvtIoDeviceControl`：

- 三个 IOCTL（`IOCTL_NPU_EXECUTE` / `IOCTL_NPU_LOAD_WEIGHTS` / `IOCTL_NPU_GET_STATUS`）的命令码如何用 `CTL_CODE` 构造（见 [driver/win32/npudriver.h:10-16](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L10-L16)）。
- `IOCTL_NPU_LOAD_WEIGHTS` 路径里 DMA 事务的创建与执行流程，以及其中 `WdfDmaTransactionCreate` / `WdfDmaTransactionInitialize` 的可疑调用（**待确认**）。
- ISR 确认中断 → DPC 延迟善后的分工细节，以及 `NpuEvtInterruptDpc` 当前的 `TODO` 缺口。

建议在进入下一讲前，先完成本讲第 5 节的综合时序图——它会让你在阅读 IOCTL 代码时始终清楚「这一步在整个生命周期里处于什么位置」。
