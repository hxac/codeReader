# 驱动 IOCTL、DMA 与中断交互

## 1. 本讲目标

承接 u5-l1 讲清的「驱动如何被加载、如何绑定到 PCI 设备、`DEVICE_CONTEXT` 与 `NPU_REGISTERS` 长什么样」。本讲再往下走一层：当驱动已经挂到设备上之后，**应用程序究竟怎样让 NPU 干活、NPU 干完活又怎样把消息通知回来**。

学完本讲，你应当能够：

- 说清 `CTL_CODE` 宏的四个组成部分，并能手算一个 IOCTL 命令码的值；
- 复述 `NpuEvtPrepareHardware` 如何把硬件寄存器映射成可读写的内存指针、并完成 NPU 复位；
- 解释 `NpuEvtIoDeviceControl` 如何用 `switch` 派发三条 IOCTL，其中 `IOCTL_NPU_LOAD_WEIGHTS` 借助 DMA 散列收集把权重搬到设备的完整流程；
- 区分中断的「顶半部」`NpuEvtInterruptIsr`（确认中断、抢时间）与「底半部」`NpuEvtInterruptDpc`（延迟做善后）的分工；
- 识别本驱动中尚未完成或写法存疑的环节，并如实标注「待确认」。

本讲覆盖四个最小模块：`NpuEvtPrepareHardware`、`NpuEvtIoDeviceControl`、`NpuEvtInterruptIsr`、`NpuEvtInterruptDpc`，全部位于 [npudriver.c](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c) 与 [npudriver.h](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h)。

## 2. 前置知识

### 2.1 用户态与内核态

Windows 把运行权限分成两层：应用程序跑在**用户态**（user mode），权限受限，不能直接碰硬件；驱动跑在**内核态**（kernel mode），能直接读写寄存器、分配物理内存。应用程序想让硬件做事，必须通过操作系统提供的「传话筒」把请求送进内核，这个传话筒就是 IOCTL。

### 2.2 IOCTL 是什么

**IOCTL**（I/O Control，输入输出控制）是 Windows 上一对一的「应用 ↔ 驱动」命令通道。应用层调用 `DeviceIoControl()` 传入一个 32 位整数 `IoControlCode`（命令码）和一段缓冲区，操作系统把这个调用转发给驱动里的 `EvtIoDeviceControl` 回调，驱动根据命令码决定做什么、怎么回填数据。命令码不是随便取的整数，而是用 `CTL_CODE` 宏按固定字段拼出来的（4.2 详述），这样不同驱动的命令码不会撞车。

### 2.3 MMIO：把寄存器当内存访问

**MMIO**（Memory-Mapped I/O，内存映射 I/O）把设备的控制寄存器映射到一段 CPU 地址空间，驱动用普通的指针读写就能控制设备——写某个地址等于给设备下命令，读某个地址等于查设备状态。本驱动用 `READ_REGISTER_ULONG` / `WRITE_REGISTER_ULONG` 这两个宏来读写，它们保证按 32 位宽度、不经过 CPU 缓存直达硬件。`NPU_REGISTERS` 结构体（u5-l1 已介绍）就是这段寄存器内存的 C 语言「图纸」。

### 2.4 DMA 与散列收集

**DMA**（Direct Memory Access，直接内存访问）让设备在不打扰 CPU 的情况下直接读写系统内存。搬一大块权重数据时，若让 CPU 一个字一个字地拷，CPU 会被占满；改用 DMA，CPU 只需「布置任务」，剩下的搬运由 DMA 控制器完成。**散列收集**（Scatter-Gather）是 DMA 的一种模式：当缓冲区在物理上被打散成多段不连续页面时，DMA 控制器能按一张「物理地址 + 长度」的列表逐段搬运，无需先把内存整理成连续物理块。

### 2.5 中断与 IRQL：顶半部 + 底半部

设备干完活（如一次卷积算完）会拉高一根中断线主动喊 CPU。Windows 内核代码总运行在某个 **IRQL**（中断请求级）上，可想象成一条优先级跑道，数字越高越优先：

| IRQL | 数值 | 谁在这里跑 | 本讲对应代码 |
|------|------|-----------|-------------|
| `PASSIVE_LEVEL` | 0 | 普通应用线程 | 发起 `DeviceIoControl` 的线程 |
| `DISPATCH_LEVEL` | 2 | 延迟过程调用（DPC） | `NpuEvtInterruptDpc` |
| DIRQL（设备级） | 更高 | 硬件中断服务 | `NpuEvtInterruptIsr` |

硬件中断到来时，系统把 CPU 拉到 DIRQL 跳进 **ISR**（中断服务例程）。因为 ISR 优先级最高、会屏蔽大量其它中断，所以必须**极短**——只做「确认这是我家的中断」；真正的善后（完成请求、推进队列）放到稍后在 `DISPATCH_LEVEL` 执行的 **DPC**（延迟过程调用）里慢慢做。这套「顶半部 ISR + 底半部 DPC」是 Windows 驱动处理中断的标准范式，根因就是 IRQL 分层。

### 2.6 与 u5-l1 的衔接

u5-l1 已讲过 `DriverEntry`、`NpuEvtDeviceAdd`（创建设备/中断/DMA enabler/IO 队列）、`DEVICE_CONTEXT`、`NPU_REGISTERS`、以及 `setup.inf` 的 PCI 绑定。本讲聚焦四个被装配进设备的回调——它们是设备真正「动起来」后会被调用的核心函数。

## 3. 本讲源码地图

永久链接 base：`https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/`

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [npudriver.h](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h) | 寄存器/上下文/IOCTL 宏定义 | `CTL_CODE` 三条命令码、`NPU_REGISTERS` 字段 |
| [npudriver.c](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c) | 驱动回调实现 | 四个回调函数的逐行逻辑 |

四个最小模块在源码中的位置：

| 模块（函数） | 所在文件 | 行号 | 职责 |
|---|---|---|---|
| `NpuEvtPrepareHardware` | npudriver.c | 73-101 | 映射寄存器、复位 NPU |
| `NpuEvtIoDeviceControl` | npudriver.c | 103-177 | IOCTL 派发（含 DMA 权重加载）|
| `NpuEvtInterruptIsr` | npudriver.c | 179-189 | 中断顶半部：确认中断 |
| `NpuEvtInterruptDpc` | npudriver.c | 191-195 | 中断底半部：延迟善后（待实现）|

## 4. 核心概念与源码讲解

按一次完整设备交互的时间顺序来讲：先把硬件准备好（4.1），再讲应用怎么下命令（4.2，含 DMA），最后讲设备怎么把「完成」通知回来（4.3、4.4）。

### 4.1 NpuEvtPrepareHardware —— 寄存器映射与硬件初始化

#### 4.1.1 概念说明

设备上电并被 PnP 管理器分配好资源后，WDF 会调用 `EvtDevicePrepareHardware` 回调。这是驱动**第一次**能真正碰到硬件的时刻：参数 `ResourcesTranslated` 里列着翻译后的物理资源（一段 MMIO 地址、一根中断线等），驱动要在此把「原始物理地址」翻译成「内核里可读写的虚拟指针」，并用这个指针去复位设备、把它带到一个已知状态。本驱动里这个回调叫 `NpuEvtPrepareHardware`。

#### 4.1.2 核心流程

```
NpuEvtPrepareHardware(Device, ResourcesRaw, ResourcesTranslated)
 ├─ 遍历 ResourcesTranslated 中每一条资源描述符
 │    └─ 若 Type == CmResourceTypeMemory（是一段 MMIO）：
 │         ├─ MmMapIoSpaceEx(物理地址, 长度, PAGE_READWRITE | PAGE_NOCACHE)
 │         └─ 把返回的虚拟地址强转成 NPU_REGISTERS* 存进 devContext->Registers
 ├─ 写 ControlStatus = 0x1            （复位 NPU）
 ├─ KeStallExecutionProcessor(100)    （忙等约 100 微秒，让复位生效）
 └─ 写 ControlStatus = 0x0            （解除复位）
```

关键点：`PAGE_NOCACHE` 表示这段内存不经过 CPU 缓存——寄存器读写必须直达硬件，缓存会让「写命令」延迟生效、让「读状态」读到旧值。

#### 4.1.3 源码精读

资源遍历与映射（注意 `MmMapIoSpaceEx` 的三参数与 `PAGE_NOCACHE`）：

[npudriver.c:79-92](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L79-L92) —— 遍历翻译后资源，找到 `CmResourceTypeMemory` 类型的条目，调用 `MmMapIoSpaceEx` 把 PCI BAR 的物理地址映射成内核虚拟地址，强转为 `NPU_REGISTERS*` 存入设备上下文；映射失败时直接返回 `STATUS_INSUFFICIENT_RESOURCES`。

复位序列：

[npudriver.c:96-98](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L96-L98) —— 先写 `ControlStatus=0x1` 复位，`KeStallExecutionProcessor(100)` 忙等约 100 微秒让复位电路稳定，再写 `0x0` 解除复位。

被映射的 `NPU_REGISTERS` 结构体字段（即寄存器「图纸」）：

[npudriver.h:19-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L19-L25) —— 定义了 5 个字段：`ControlStatus`（控制/状态）、`InterruptMask`（中断屏蔽/确认）、`DmaSource`/`DmaDest`（DMA 源/目的地址，64 位）、`DmaLength`（DMA 传输长度）。各字段的**顺序与位宽**就是它们在 BAR 窗口里的物理字节偏移（如 `ControlStatus@0x0`、`InterruptMask@0x4`、`DmaSource@0x8`、`DmaDest@0x10`、`DmaLength@0x18`），必须与硬件 RTL 一致。

注意这里只处理了 `CmResourceTypeMemory`，**没有处理中断资源**——中断的连接由 WDF 在 `WdfInterruptCreate` 之后自动完成（见 u5-l1 的 `NpuEvtDeviceAdd`），所以这里不必手动连中断。

> 🟡 待确认：本映射假设整段 BAR 严格按 `NPU_REGISTERS` 字段顺序排列，但代码**没有任何长度校验**（没核对 `resource->u.Memory.Length >= sizeof(NPU_REGISTERS)`），若设备 BAR 比结构体小，后续读写会越界。若 `ResourcesTranslated` 里根本没有 `CmResourceTypeMemory` 条目，则 `Registers` 保持 NULL，后续任何 `WRITE_REGISTER_ULONG` 都会崩溃——这是驱动隐含假设硬件资源正常的脆弱点。

#### 4.1.4 代码实践

**目标**：理解 `MmMapIoSpaceEx` 的输入输出，看清「物理地址 → 虚拟指针 → 结构体字段」这条链。

**步骤**：

1. 打开 [npudriver.c:79-92](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L79-L92)，确认 `resource->u.Memory.Start`（物理地址）和 `resource->u.Memory.Length`（长度）都来自 PnP 资源描述符。
2. 打开 [npudriver.h:19-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L19-L25)，对照 `NPU_REGISTERS` 五个字段，手算它们在 BAR 中的字节偏移。
3. 在纸上演算：若 `Length = 0x1000`（4 KiB），而 `NPU_REGISTERS` 只占约 28 字节，那么 `Registers->DmaLength` 之后的大量映射空间当前未被使用——这些「留白」对应设备未来的寄存器扩展位。

**现象与预期结果**：映射成功后 `devContext->Registers` 非 NULL，后续所有对设备的操作都通过这个指针完成。具体读回值依赖硬件，**待本地验证**（本仓库无 WDK 构建环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MmMapIoSpaceEx` 必须带 `PAGE_NOCACHE`，而普通内存分配（如 `WdfMemoryCreate`）不需要？

> **参考答案**：普通内存里的数据是「数据」，缓存能加速访问且最终会写回，不影响正确性。但寄存器是「控制接口」——写 `ControlStatus=0x2` 是给设备下「开始计算」的命令，若被缓存暂存，命令不会立即送达设备；读 `ControlStatus` 查状态时，缓存会返回旧值。`PAGE_NOCACHE` 强制每次读写直达硬件，保证控制语义即时生效。

**练习 2**：`KeStallExecutionProcessor(100)` 是忙等还是睡眠？它能在什么 IRQL 调用？

> **参考答案**：是**忙等**（spin-wait），CPU 在原地空转计时不让出。它专为极短延时设计，可在 `DISPATCH_LEVEL` 甚至更高 IRQL 调用。这里用它而非睡眠，是为了保证复位时序精确、不依赖线程调度——这正是「复位必须等够」这种微秒级时序的典型用法。

---

### 4.2 NpuEvtIoDeviceControl —— IOCTL 派发与 DMA 权重加载

#### 4.2.1 概念说明

这是驱动里最核心的交互入口。每当应用调用 `DeviceIoControl()` 下发命令，IO 管理器把请求排进 u5-l1 创建的默认 IO 队列，WDF 随后调用 `NpuEvtIoDeviceControl`，把命令码 `IoControlCode`、输入缓冲、输出缓冲都交给你。驱动用一个 `switch` 把不同命令码分派到不同分支。Mayoiuta 定义了三条命令：

| IOCTL | 含义 | 方向 |
|---|---|---|
| `IOCTL_NPU_EXECUTE` | 启动一次 NPU 计算 | 应用 → 设备（写命令）|
| `IOCTL_NPU_LOAD_WEIGHTS` | 用 DMA 把权重数据搬到设备 | 应用 → 设备（搬数据）|
| `IOCTL_NPU_GET_STATUS` | 读取设备状态寄存器 | 设备 → 应用（读状态）|

#### 4.2.2 核心流程：CTL_CODE 怎么拼

三条命令码在头文件用 `CTL_CODE` 宏定义：

[npudriver.h:10-16](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L10-L16) —— 以 `NPU_IOCTL_BASE = 0x800` 作功能号基准，三条命令功能号依次为 `0x800/0x801/0x802`，统一用 `FILE_DEVICE_UNKNOWN`、`METHOD_BUFFERED`、`FILE_ANY_ACCESS`。

`CTL_CODE(DeviceType, Function, Method, Access)` 把四个字段拼成一个 32 位整数：

\[
\text{IOCTL} = (\text{DeviceType} \ll 16)\;|\;(\text{Access} \ll 14)\;|\;(\text{Function} \ll 2)\;|\;\text{Method}
\]

各字段含义：

- **DeviceType**（位 16-31）：设备类型。`FILE_DEVICE_UNKNOWN = 0x22`，自定义设备常用值。
- **Access**（位 14-15）：访问权限。`FILE_ANY_ACCESS = 0`，任何权限句柄都能用。
- **Function**（位 2-13）：功能号。这里用 `NPU_IOCTL_BASE + 偏移`，`0x800` 是自定义驱动的「功能区段」。
- **Method**（位 0-1）：数据传递方式。`METHOD_BUFFERED = 0`，系统用单个中间缓冲区中转数据，最简单也最常用。

手算 `IOCTL_NPU_EXECUTE`（按 WDK 宏展开估算，**待本地验证**）：

\[
(0\text{x}22 \ll 16)\;|\;(0 \ll 14)\;|\;(0\text{x}800 \ll 2)\;|\;0 = 0\text{x}220000\;|\;0\text{x}2000 = 0\text{x}00222000
\]

同理 `IOCTL_NPU_LOAD_WEIGHTS = 0x00222004`、`IOCTL_NPU_GET_STATUS = 0x00222008`。三条命令码相邻递增 4（因为 `Method` 占 2 位、功能号在高位左移 2），这正是 `Function << 2` 的效果。

`METHOD_BUFFERED` 的含义：I/O 管理器自动分配一块**系统缓冲区**（大小取输入、输出的较大值），调用驱动前把用户输入拷进这块缓冲区，驱动完成请求后再把它拷回用户输出缓冲区。驱动全程只碰这块内核缓冲，不直接碰用户态内存——既安全又简单，代价是多一次拷贝。

#### 4.2.3 核心流程：三个 case 各做什么

```
NpuEvtIoDeviceControl(Queue, Request, ..., IoControlCode)
 ├─ device = WdfIoQueueGetDevice(Queue)
 ├─ devContext = GetDeviceContext(device)
 ├─ switch (IoControlCode):
 │    case IOCTL_NPU_EXECUTE:
 │         ├─ WdfSpinLockAcquire(DmaLock)                  // 保护寄存器访问
 │         ├─ WRITE_REGISTER_ULONG(ControlStatus, 0x2)     // 启动计算
 │         └─ WdfSpinLockRelease(DmaLock)
 │         length = 0
 │    case IOCTL_NPU_LOAD_WEIGHTS:  ← 见 4.2.5 DMA 子流程
 │    case IOCTL_NPU_GET_STATUS:
 │         ├─ statusReg = READ_REGISTER_ULONG(ControlStatus)
 │         ├─ WdfRequestCopyBuffer(Request, &statusReg, 4)  // 回填输出缓冲
 │         └─ length = 4
 │    default: status = STATUS_INVALID_DEVICE_REQUEST
 └─ WdfRequestCompleteWithInformation(Request, status, length)
```

末尾 `WdfRequestCompleteWithInformation` 统一完成请求——无论命中哪个分支，请求都在函数末尾被一次性完成，这是 WDF 推荐写法。

#### 4.2.4 源码精读：EXECUTE 与 GET_STATUS

派发入口与 `EXECUTE` 分支：

[npudriver.c:103-118](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L103-L118) —— 取出设备与上下文，进入 `switch`；`IOCTL_NPU_EXECUTE` 分支在自旋锁 `DmaLock` 保护下向 `ControlStatus` 写 `0x2` 启动计算。

`GET_STATUS` 分支：

[npudriver.c:164-169](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L164-L169) —— 读 `ControlStatus` 寄存器，用 `WdfRequestCopyBuffer` 把 4 字节状态值拷进请求的输出缓冲，`length` 设为 `sizeof(ULONG)`。这是最简单的「设备 → 应用」数据回传。

统一完成请求：

[npudriver.c:176](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L176) —— `WdfRequestCompleteWithInformation(Request, status, length)` 用状态码和传输字节数完成请求，把控制权还给应用层。

#### 4.2.5 DMA 权重加载子流程（IOCTL_NPU_LOAD_WEIGHTS）

这是三条命令里最复杂的一条，也是本讲的一个重点。它要用 DMA 把应用传来的权重缓冲区搬到设备。

```
case IOCTL_NPU_LOAD_WEIGHTS:
 ├─ WdfRequestRetrieveInputBuffer(Request, 0, &inputBuffer, &bufferSize)
 │     // 从请求里取出应用的输入缓冲指针与长度
 ├─ WdfDmaTransactionCreate(DmaEnabler,
 │     WDF_DMA_ENABLER_CONFIG_REQUIRE_SINGLE_TRANSFER, ...)   // 建一个 DMA 事务
 ├─ WdfMemoryCreateFromBuffer(inputBuffer, bufferSize, &dmaMemory)
 │     // 把应用的缓冲包成 WDFMEMORY 对象
 ├─ WdfDmaTransactionInitialize(
 │     dmaTransaction, WdfDmaDirectionWriteToDevice,
 │     buffer, bufferSize,
 │     devContext->Registers->DmaSource,   // ← 读寄存器值当参数
 │     devContext->Registers->DmaDest)     // ← 读寄存器值当参数
 ├─ WdfDmaTransactionExecute(dmaTransaction, WDF_NO_CONTEXT)
 ├─ WdfObjectDelete(dmaTransaction)        // 立即删除事务
 └─ length = bufferSize
```

**DMA enabler 从哪来**：在 u5-l1 的 `NpuEvtDeviceAdd` 里已创建好——

[npudriver.c:54-60](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L54-L60) —— 用 `WdfDmaProfileScatterGather64` 配置 64 位散列收集 DMA enabler，最大传输长度 64，存入 `devContext->DmaEnabler`。这正是「散列收集」模式的来源：设备能处理散落的不连续内存页。

DMA 子流程源码：

[npudriver.c:120-162](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L120-L162) —— 完整的权重加载分支：取输入缓冲 → 建事务 → 包内存 → 初始化（方向为 `WriteToDevice`）→ 执行 → 删除事务。`WdfDmaDirectionWriteToDevice` 表明数据从主机内存流向设备。

#### 4.2.6 待确认：IOCTL / DMA 路径的几处缺口

对照 WDF 散列收集 DMA 的标准用法，本驱动的 DMA 路径更像「示意草图」，有几处需如实标注：

1. **`DmaLock` 从未创建**：`DEVICE_CONTEXT.DmaLock`（[npudriver.h:33](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L33)）被 `IOCTL_NPU_EXECUTE` 获取/释放（[npudriver.c:113-115](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L113-L115)），但全文件**没有** `WdfSpinLockCreate` 调用（已用搜索确认）。一个未初始化的自旋锁被获取会直接蓝屏——这是遗漏的初始化。
2. **DMA 事务同步删除**：`WdfDmaTransactionExecute` 后紧接着 `WdfObjectDelete`（[npudriver.c:158-159](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L158-L159)），并在函数末尾立刻 `WdfRequestCompleteWithInformation` 完成请求。标准散列收集 DMA 是**异步**的：`Execute` 返回时搬运多半还没完成，需要注册 `EvtProgramDma` 回调来真正编程设备 DMA 引擎，并在传输完成的中断里调用 `WdfDmaTransactionDmaCompleted` 收尾。本驱动既没有 `EvtProgramDma`、又没有把请求延迟到 DMA 完成时再完成，传输真正结束前事务对象就被销毁、应用就被通知「加载完成」。
3. **WDF 调用签名与公开文档不符**：标准 `WdfDmaTransactionCreate` 第二参数应是 `PWDF_OBJECT_ATTRIBUTES`，而这里传的是 `WDF_DMA_ENABLER_CONFIG_REQUIRE_SINGLE_TRANSFER`（一个属于 `WDF_DMA_ENABLER_CONFIG.Flags` 的标志位）；`WdfDmaTransactionInitialize` 标准签名需要 `EvtProgramDmaFunction` 回调与 `WDFMEMORY`，而这里传的是原始缓冲指针；`WdfMemoryCreateFromBuffer` 的标准参数顺序是 `(Attributes, BufferSize, Buffer, &Memory)`，这里写成 `(Attributes, inputBuffer, bufferSize, &dmaMemory)`，`BufferSize` 与 `Buffer` 位置疑似颠倒。三处调用与公开 WDF 文档对不上。
4. **寄存器值被当 DMA 参数传入**：`WdfDmaTransactionInitialize` 的最后两个参数取自 `devContext->Registers->DmaSource` / `DmaDest`（[npudriver.c:155-156](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L155-L156)），即**读**寄存器当前值当 DMA 源/目的地址。常规做法恰恰相反——驱动应把主机缓冲的物理地址**写**进 `DmaSource`、把设备侧目的地址**写**进 `DmaDest`，以此编程设备 DMA 引擎。这里的读写方向像是反了。
5. **`DmaLength` 寄存器从未写入**：[npudriver.h:24](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L24) 声明了 `DmaLength`，但全代码从未对其赋值（已用搜索确认）。设备如何知道本次要搬多少字节，**待确认**。

这些缺口与 u4-l3 指出的「模块齐全、互连缺失」整体定性一致：本驱动是教学/示意级骨架，重在讲清交互意图，而非可直接编译运行的成品。

#### 4.2.7 代码实践

**目标**：亲手加一条新 IOCTL `IOCTL_NPU_SET_CONFIG`，走通「定义命令码 → 派发 → 写寄存器」全链路。

**步骤**：

1. 在 [npudriver.h:11-16](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L11-L16) 之后新增一行，功能号取 `NPU_IOCTL_BASE + 0x3`：
   ```c
   // 示例代码：新增的配置 IOCTL
   #define IOCTL_NPU_SET_CONFIG \
       CTL_CODE(FILE_DEVICE_UNKNOWN, NPU_IOCTL_BASE + 0x3, METHOD_BUFFERED, FILE_ANY_ACCESS)
   ```
   手算其值应为 `0x0022200C`（功能号 `0x803`，`0x803 << 2 = 0x200C`，再 `| 0x220000`）。
2. 在 [npudriver.c:103-177](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L103-L177) 的 `switch` 里、`default` 之前插入新分支：从输入缓冲取出一个 `ULONG` 配置值，写入一个配置寄存器（这里复用 `InterruptMask` 字段作示例存储位）：
   ```c
   // 示例代码：新增 case
   case IOCTL_NPU_SET_CONFIG: {
       ULONG config;
       status = WdfRequestRetrieveInputBuffer(Request, sizeof(ULONG), (PVOID*)&config, NULL);
       if (!NT_SUCCESS(status)) break;
       WRITE_REGISTER_ULONG(&devContext->Registers->InterruptMask, config);
       length = 0;
       break;
   }
   ```
3. 对照已有 `IOCTL_NPU_GET_STATUS` 分支（[npudriver.c:164-169](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L164-L169)）确认输入缓冲读取与 `WRITE_REGISTER_ULONG` 用法一致；确认末尾 [npudriver.c:176](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L176) 的 `WdfRequestCompleteWithInformation` 会自动覆盖新分支。

**现象与预期结果**：编译后（**待本地验证**——本仓库未提供 WDK 构建脚本），应用层用 `DeviceIoControl(handle, IOCTL_NPU_SET_CONFIG, &configVal, 4, NULL, 0, &returned, NULL)` 下发，驱动应把 `configVal` 写入 `InterruptMask` 寄存器并成功返回。由于 `DmaLock` 未初始化的缺口仍在，请勿在此新分支里使用 `WdfSpinLockAcquire`，以免触发 4.2.6 第 1 点的蓝屏风险。

#### 4.2.8 小练习与答案

**练习 1**：若把 `IOCTL_NPU_SET_CONFIG` 的 `Method` 从 `METHOD_BUFFERED` 改成 `METHOD_NEITHER`（=3），对驱动写法有什么影响？

> **参考答案**：`METHOD_BUFFERED` 下系统把输入/输出数据拷进一个内核中转缓冲，驱动用 `WdfRequestRetrieveInputBuffer` 拿到的是这块安全缓冲的指针。改成 `METHOD_NEITHER` 后，系统直接把应用进程的虚拟地址原样传给驱动，驱动必须自己在正确的进程上下文里访问、并做 `ProbeForRead/Write` 校验，否则跨进程会读到无效地址。`METHOD_BUFFERED` 简单安全，`METHOD_NEITHER` 省一次拷贝但门槛高。

**练习 2**：为什么 `IOCTL_NPU_EXECUTE` 要在写 `ControlStatus` 前后加自旋锁，而 `IOCTL_NPU_GET_STATUS` 没加？

> **参考答案**：`EXECUTE` 写的是「启动计算」这种**会改变设备状态**的命令，若多个线程同时下发，写操作交错可能导致命令丢失或状态错乱，需要自旋锁串行化。`GET_STATUS` 只读寄存器，读操作天然幂等、不改变设备状态，并发读不会互相破坏，因此无需加锁。

**练习 3**：算一算 `IOCTL_NPU_GET_STATUS` 的命令码十六进制值。

> **参考答案**：功能号 = `0x800 + 0x2 = 0x802`，`0x802 << 2 = 0x2008`，拼上 `0x22 << 16 = 0x220000`，得 `0x00222008`。

---

### 4.3 NpuEvtInterruptIsr —— 中断顶半部

#### 4.3.1 概念说明

当 NPU 算完一次任务（或出错）拉高中断线，CPU 在很高的 IRQL（`DIRQL`，设备中断级）跳进 ISR。ISR 必须**极短**：不能等待、不能调 pageable 代码、不能做耗时操作。标准做法是「读状态判断是不是我家的设备产生的中断 → 若是，确认（ack）中断让硬件把中断线放下 → 请求调度一个 DPC」。`NpuEvtInterruptIsr` 就是这套范式里的顶半部。

#### 4.3.2 核心流程

```
NpuEvtInterruptIsr(Interrupt, MessageID)
 ├─ devContext = GetDeviceContext(WdfInterruptGetDevice(Interrupt))
 ├─ status = READ_REGISTER_ULONG(ControlStatus)
 ├─ if (status & 0x80000000):        // 最高位为中断标志
 │     ├─ WRITE_REGISTER_ULONG(InterruptMask, 0x1)   // 确认/屏蔽中断
 │     ├─ (标准做法：WdfInterruptQueueDpcForIsr 排一个 DPC)
 │     └─ return TRUE                // 「这是我处理的」
 └─ return FALSE                     // 不是我的中断，让别的驱动处理
```

返回 `TRUE` 表示中断已认领，IO 管理器不再往下游驱动传；返回 `FALSE` 表示「不是我家的」，让共享中断线的其他驱动继续判断（典型于 PCI 共享中断）。

#### 4.3.3 源码精读

[npudriver.c:179-189](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L179-L189) —— ISR 读取 `ControlStatus`，用 `status & 0x80000000` 判断最高位（bit 31）是否为中断标志位。若是，向 `InterruptMask` 写 `0x1` 确认中断（让设备放下中断线，避免重复触发），并返回 `TRUE`。

ISR 与 DPC 的注册在 u5-l1 的 `NpuEvtDeviceAdd` 里成对完成——

[npudriver.c:47-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L47-L49) —— `WDF_INTERRUPT_CONFIG_INIT(&interruptConfig, NpuEvtInterruptIsr, NpuEvtInterruptDpc)` 同时把 ISR（顶半部）和 DPC（底半部）绑在一起，这正是「顶半 + 底半」成对出现的来源。

> 🟡 待确认（中断顶半部的几处疑点）：
>
> 1. **`InterruptMask` 语义存疑**：寄存器名叫「Mask（屏蔽）」，惯例上写 1 通常表示**屏蔽/关闭**该中断，而非「确认」。把它当 ack 用、且之后**从不重新打开**，可能导致后续中断被永久屏蔽。它到底是 ack 寄存器还是 mask 寄存器，需对照硬件文档确认。
> 2. **bit 31 是魔数**：`0x80000000` 对应 `ControlStatus` 的 bit 31，但 [npudriver.h:19-25](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.h#L19-L25) 没有为它定义宏，它究竟代表「完成」还是「出错」，目前只能从 ISR 行为反推为「有中断待处理」。

#### 4.3.4 代码实践

**目标**：通过阅读 ISR 与 DPC 的注册关系，理解「顶半部只确认、底半部才善后」的分工，并发现「DPC 从未被排队」这一隐藏断点。

**步骤**：

1. 读 [npudriver.c:47-49](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L47-L49)，确认 `WDF_INTERRUPT_CONFIG_INIT` 的第二、三参数就是 `NpuEvtInterruptIsr`、`NpuEvtInterruptDpc`。
2. 对照 [npudriver.c:179-189](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L179-L189) 的 ISR：它只做了「读状态 → 判位 → 写掩码 → 返回」，**没有**完成任何 IO 请求、**没有**显式排队 DPC。
3. （搜索验证）用 grep 在 `driver/win32/` 下搜索 `WdfInterruptQueueDpcForIsr`——确认全仓库**没有**这一调用。这意味着即便 ISR 返回 `TRUE`，`NpuEvtInterruptDpc` 也永远不会被触发（WDF 不会因 ISR 返回 TRUE 就自动跑 DPC，必须显式排队）。

**现象与预期结果**：在没有真实硬件的情况下无法实际触发中断，属「源码阅读型实践」。预期读者能画出 `EXECUTE 启动 → 硬件计算 → 拉中断 → ISR 确认 →（断）→ DPC 完成` 的时序，并指出两个断点：DPC 从未被排队、DPC 体为空。

#### 4.3.5 小练习与答案

**练习 1**：ISR 为什么返回 `FALSE`？返回 `FALSE` 后系统会做什么？

> **参考答案**：返回 `FALSE` 表示「这个中断不是我驱动的设备产生的」。系统会把中断机会让给共享同一中断线的其他驱动继续判断。若所有驱动都返回 `FALSE`，系统会记录「未处理中断」并可能禁用该中断线以防风暴。

**练习 2**：为什么把「确认中断」放在 ISR 而不放到 DPC 里？

> **参考答案**：确认中断（写 `InterruptMask`）必须**立刻**做，否则设备的中断线一直保持有效，CPU 一退出 ISR 就会被同一中断再次打断，形成中断风暴。DPC 是延迟执行的，等它跑起来时设备可能已经触发了成百上千次重复中断。所以「让硬件放下中断线」这件最紧急的事必须在 ISR 里完成，DPC 只负责不紧急的善后。

**练习 3**：本驱动即使设备真的发出中断，`NpuEvtInterruptDpc` 也几乎不会运行，为什么？

> **参考答案**：因为 ISR 从未调用 `WdfInterruptQueueDpcForIsr` 来请求调度 DPC。WDF 不会因为「ISR 返回 `TRUE`」就自动跑 DPC；DPC 必须由 ISR 显式排队。所以「ISR 确认 → DPC 善后」这条链路在第二步是断开的。

---

### 4.4 NpuEvtInterruptDpc —— 中断底半部

#### 4.4.1 概念说明

ISR 跑在高 IRQL，很多事做不了（如完成 IO 请求、访问 pageable 内存、调用部分 WDF API）。WDF 的做法是：ISR 里请求把一个 DPC 排入队列；系统随后在 `DISPATCH_LEVEL`（比 `DIRQL` 低）执行这个 DPC。DPC 里做「不紧急但必要」的善后：完成被挂起的 IO 请求、推进队列、通知应用。`NpuEvtInterruptDpc` 就是这个底半部。

#### 4.4.2 核心流程（设计意图 vs 现状）

**设计意图**：

```
NpuEvtInterruptDpc(Interrupt, AssociatedObject)
 ├─ devContext = GetDeviceContext(WdfInterruptGetDevice(Interrupt))
 ├─ 取出 ISR 期间挂起的请求（如 IOCTL_NPU_EXECUTE 对应的 Request）
 ├─ WdfRequestCompleteWithInformation(Request, STATUS_SUCCESS, length)
 └─ 若有完成队列，推进下一批
```

**现状**：

[npudriver.c:191-195](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L191-L195) —— 函数体只有取上下文一句和一句 `// TODO: 实现完成队列处理` 注释，**完全没有实现善后逻辑**。这是本驱动最明确的「待实现」环节。

#### 4.4.3 源码精读

注意 DPC 的签名第二参数是 `AssociatedObject`（通常是中断对象或设备对象），而 ISR 的第二参数是 `MessageID`（消息信号中断的编号）——两者签名不同，反映了它们由框架在不同时机以不同参数调用。DPC 与 ISR 的配对关系在 u5-l1 的 `NpuEvtDeviceAdd` 里绑定（[npudriver.c:47](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L47)，已在 4.3.3 引用）。

#### 4.4.4 代码实践

**目标**：为空的 DPC 设计一个最小完成逻辑，把「EXECUTE 启动 → 中断完成」这条断链补上。

**步骤**：

1. 读 [npudriver.c:191-195](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L191-L195)，确认 DPC 当前为空。
2. 设计：由于 `IOCTL_NPU_EXECUTE`（4.2）在启动计算后**没有保存 Request** 就走了 `WdfRequestCompleteWithInformation`（[npudriver.c:176](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L176)），DPC 其实无请求可完成。要补上断链，需先把 `EXECUTE` 分支改成「保存 Request 不立即完成」，并在 ISR 里排队 DPC，再在 DPC 里 `WdfRequestComplete`。
3. 在纸上写出最小改法（**示例代码，不实际改源码**）：
   ```c
   // 示例代码：ISR 末尾排队 DPC（接在 WRITE_REGISTER_ULONG(InterruptMask, 0x1) 之后）
   WdfInterruptQueueDpcForIsr(Interrupt);

   // 示例代码：EXECUTE 分支保存请求，不走末尾的统一完成
   case IOCTL_NPU_EXECUTE: {
       WdfSpinLockAcquire(devContext->DmaLock);   // 注意 DmaLock 待初始化（4.2.6）
       devContext->PendingRequest = Request;       // 新增字段，暂存请求
       WRITE_REGISTER_ULONG(&devContext->Registers->ControlStatus, 0x2);
       WdfSpinLockRelease(devContext->DmaLock);
       return;   // 关键：不落到末尾的 WdfRequestCompleteWithInformation
   }

   // 示例代码：DPC 里完成请求
   VOID NpuEvtInterruptDpc(WDFINTERRUPT Interrupt, WDFOBJECT AssociatedObject) {
       PDEVICE_CONTEXT devContext = GetDeviceContext(WdfInterruptGetDevice(Interrupt));
       WDFREQUEST req = devContext->PendingRequest;
       devContext->PendingRequest = NULL;
       if (req) WdfRequestCompleteWithInformation(req, STATUS_SUCCESS, 0);
   }
   ```

**现象与预期结果**：纯设计型实践，**待本地验证**。预期读者理解：当前 `EXECUTE` 在启动计算的同一函数里就完成了请求，应用拿到的「完成」其实只代表「命令已下发」，并不代表「计算已结束」——这是一个语义偏差，是 DPC 未实现 + ISR 未排队 DPC 共同带来的副作用。

#### 4.4.5 小练习与答案

**练习 1**：ISR 跑在 `DIRQL`，DPC 跑在 `DISPATCH_LEVEL`。为什么说「把工作从 ISR 挪到 DPC」能提升系统整体响应？

> **参考答案**：`DIRQL` 高于 `DISPATCH_LEVEL`，会屏蔽更多中断。ISR 占着 `DIRQL` 的时间越长，别的设备的中断就被压制越久，系统实时性下降。把能推迟的工作挪到 `DIRQL` 退出后的 DPC 做，能让 ISR 极快返回、尽快放开中断屏蔽，其它设备得以迅速响应；DPC 里的善后虽稍迟，但不影响中断实时性。

**练习 2**：当前 DPC 是空实现，对 `IOCTL_NPU_EXECUTE` 的「完成语义」有什么影响？

> **参考答案**：因为 `EXECUTE` 在下发命令后立即 `WdfRequestCompleteWithInformation`（[npudriver.c:176](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/driver/win32/npudriver.c#L176)），应用收到完成时 NPU 可能还没算完。应用层无法通过「请求完成」来得知「计算真正结束」——正确做法应是 ISR 确认中断、DPC 里才完成请求。空 DPC 使这条「硬件完成 → 通知应用」的链路断裂，应用的完成语义被提前了。

---

## 5. 综合实践

把本讲四个模块串成一次完整的设备交互时序。

**任务**：绘制一张「NPU 一次计算」的端到端时序图，横轴是时间，纵轴是参与方（应用 / IO 管理器 / 驱动回调 / 硬件寄存器 / 中断），标出以下事件并用箭头连起来：

1. 应用 `DeviceIoControl(IOCTL_NPU_LOAD_WEIGHTS)` 下发权重 → `NpuEvtIoDeviceControl` 取缓冲 → 建 DMA 事务 → 执行 DMA（标「待确认：异步完成未实现、`DmaLength` 未写」）；
2. 应用 `DeviceIoControl(IOCTL_NPU_EXECUTE)` → `NpuEvtIoDeviceControl` 写 `ControlStatus=0x2` 启动（标「待确认：`DmaLock` 未初始化」）；
3. 硬件计算完成，拉中断 → `NpuEvtInterruptIsr` 读 `ControlStatus` 判 bit 31、写 `InterruptMask=0x1` 确认、返回 `TRUE`（标「待确认：未调用 `WdfInterruptQueueDpcForIsr`」）；
4. 框架本应排 DPC → `NpuEvtInterruptDpc`（标「待实现：完成队列 TODO」）；
5. 用虚线标出「应完成但未完成」的环节：DPC 本应完成的 `EXECUTE` 请求。

**验收标准**：图上至少出现 5 处「待确认/待实现」标记，与 4.2.6、4.3.3、4.4.2 的描述一致；能清楚区分「应用下发的命令」与「设备主动上报的中断」两条相反方向的信息流。

## 6. 本讲小结

- **IOCTL 是应用 ↔ 驱动的命令通道**：命令码由 `CTL_CODE(DeviceType, Function, Method, Access)` 四字段拼成，Mayoiuta 三条命令功能号 `0x800/0x801/0x802`、`METHOD_BUFFERED` 走系统中转缓冲。
- **`NpuEvtPrepareHardware`** 把 PnP 给的 MMIO 物理地址用 `MmMapIoSpaceEx`（`PAGE_NOCACHE`）映射成 `NPU_REGISTERS*`，再写 `ControlStatus` 复位 NPU。
- **`NpuEvtIoDeviceControl`** 用 `switch` 派发三命令：`EXECUTE` 写 `0x2` 启动、`LOAD_WEIGHTS` 走散列收集 DMA、`GET_STATUS` 读寄存器回填，末尾统一 `WdfRequestCompleteWithInformation`。
- **DMA 权重加载**用 `WdfDmaProfileScatterGather64` enabler + 事务 + `WdfDmaDirectionWriteToDevice`，但事务同步删除、无 `EvtProgramDma`、寄存器值当参数传入、`DmaLength` 从未写入，多处与标准 WDF 用法不符，属示意草图。
- **中断顶半部 `NpuEvtInterruptIsr`** 读 `ControlStatus` 判 bit 31、写 `InterruptMask` 确认、返回 `TRUE` 认领中断，但**未排队 DPC**；**底半部 `NpuEvtInterruptDpc`** 是 TODO 空壳。
- **顶半 + 底半分工**：ISR 在 `DIRQL` 只做确认（防止中断风暴），DPC 在 `DISPATCH_LEVEL` 做善后（完成请求）；当前 DPC 既不被排队、又无实现，导致 `EXECUTE` 的「完成语义」被提前。
- 全驱动至少六处「待确认」：`DmaLock` 未创建、DMA 异步完成链缺失、WDF 调用签名异常、寄存器值当参数传入、`DmaLength` 未写、ISR 未排队 DPC 且 DPC 空实现——与项目整体「模块齐全、互连缺失」的定性一致。

## 7. 下一步学习建议

本讲是 u5 单元（设备驱动）的第二篇，也是整个学习手册的收尾篇。建议：

1. **回看 u5-l1**：若尚未读 `DriverEntry`/`NpuEvtDeviceAdd`/`DEVICE_CONTEXT`/`setup.inf`，回头补齐「驱动如何被加载并绑定硬件」的完整图景——本讲的四个回调都挂在 u5-l1 搭好的骨架上。
2. **补齐 DMA 与 DPC**：若想把这份示意驱动推向可编译运行，下一步是 (a) 用 `WdfSpinLockCreate` 初始化 `DmaLock`；(b) 为 DMA 事务注册 `EvtProgramDma`、写入 `DmaLength`、并在完成中断里 `WdfDmaTransactionDmaCompleted`；(c) 在 ISR 里 `WdfInterruptQueueDpcForIsr`、在 DPC 里完成 `EXECUTE` 请求。这三处正是本讲标出的「待确认」点。
3. **对照 RTL 侧**：驱动的 `ControlStatus` bit 31（中断标志）、`0x2`（启动计算）、`DmaSource/DmaDest/DmaLength` 寄存器对应硬件侧哪些状态机与接口，可回看 u4-l3（全系统数据通路）与 u1-l3（`NPU_SOC`）把「软件命令 ↔ 硬件寄存器」对上号——目前两端均待对齐确认。
4. **扩展阅读**：微软 WDF 文档的 DMA 部分（`WdfDmaTransactionInitialize`、`EvtProgramDma`、scatter-gather 模式）与中断部分（`WDF_INTERRUPT_CONFIG`、`WdfInterruptQueueDpcForIsr`、ISR/DPC 配对），用以校准本驱动中偏离标准用法的几处调用。
