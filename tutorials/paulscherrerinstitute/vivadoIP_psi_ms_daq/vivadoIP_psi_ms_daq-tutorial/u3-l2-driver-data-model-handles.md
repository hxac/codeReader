# 驱动数据模型与句柄抽象

## 1. 本讲目标

上一讲（u3-l1）我们建立了寄存器地址模型——CPU 通过 AXI Slave 读写哪些字节地址能控制 IP。但寄存器地址只是「地址」，驱动还需要一套「软件状态」来记住：

- 这块 IP 的基址、最大流数、最大窗口数是多少？
- 第 3 条流配置过没有？它有几个窗口、数据宽度多少字节？
- 这条流上次处理到第几个窗口？哪些窗口的回调已经调用过？
- 中断到来时该回调哪个用户函数？

本讲就回答这些问题。读完本讲，你应当能够：

1. 说清楚 `PsiMsDaq_IpHandle`、`PsiMsDaq_StrHandle` 这两个 `void*` 句柄背后到底是什么数据结构。
2. 看懂 IP 实例结构体 `PsiMsDaq_Inst_t`（「一台 IP」）和流实例结构体 `PsiMsDaq_StrInst_t`（「一条流」）各自的字段职责。
3. 解释流实例里的回指指针 `str->ipHandle` 为什么必不可少。
4. 区分「堆上、长生命周期的句柄」与「栈上、瞬时存在的 `PsiMsDaq_WinInfo_t`」。
5. 理解为什么读写内存与寄存器的三个函数被做成可注入的函数指针（`AccessFct_t`）。

## 2. 前置知识

在进入源码前，先建立三个 C 语言层面的直觉：

**(1) 不透明指针（opaque pointer / PIMPL 思想）。**
C 里常用 `typedef void* FooHandle;` 把一个结构体指针藏起来。头文件只暴露 `void*`，真正的 `struct` 定义放在 `.c` 文件里。这样做的好处是：

- 用户拿不到指针背后的字段，只能通过 API 操作，**避免误用**。
- 结构体布局可以日后修改，**ABI（二进制接口）稳定**。
- 头文件依赖更少（不用在 `.h` 里 `#include` 一堆东西）。

代价是：每次用都要在 `.c` 里把 `void*` 强制转换回真实类型。

**(2) 函数指针（function pointer）。**
`typedef void RegWrite_f(uint32_t addr, uint32_t value);` 定义了一个**函数类型**，`RegWrite_f*` 才是指向这种函数的指针。把函数指针存进结构体字段，运行时再调用，就能让「写寄存器」这个动作的具体实现**可替换**——这是 C 里实现「策略注入」「依赖反转」的标准手段。

**(3) 堆（heap）vs 栈（stack）的生命周期。**

- `malloc` 分配的内存在堆上，生命周期由你显式 `free` 控制，跨函数调用长期有效。本讲的「句柄」指向的对象都在堆上。
- 函数内的局部变量在栈上，函数返回即失效。本讲的 `PsiMsDaq_WinInfo_t` 故意做成值类型、在栈上构造、按值传递，所以**用完即弃、不能长期持有**。

承接 u3-l1：上一讲我们把寄存器空间分成通用/逐流录制/逐流上下文/窗口四块；本讲的「流实例」就是软件侧用来记住「这条流在逐流和上下文区里具体占了哪些地址、配置了什么参数」的账本。

## 3. 本讲源码地图

本讲只涉及驱动 C 源码两个文件（参见 u1-l2 的「四个必记路径」）：

| 文件 | 本讲关注的内容 |
| --- | --- |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h) | 对外可见的 `typedef`：句柄、函数指针类型、`PsiMsDaq_WinInfo_t`、`PsiMsDaq_AccessFct_t`、`PsiMsDaq_StrConfig_t`、`PsiMsDaq_RetCode_t` |
| [drivers/psi_ms_daq_axi/src/psi_ms_daq.c](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c) | 对内私有的两个结构体 `PsiMsDaq_StrInst_t`、`PsiMsDaq_Inst_t`，以及 `PsiMsDaq_Init`、`PsiMsDaq_GetStrHandle` 等把它们与句柄连接起来的函数 |

一句话区分：**`.h` 里只有「类型」和「void 句柄」，`.c` 里才有「真实结构体」与「堆分配」。** 这正是信息隐藏（opaque pointer）的体现。

## 4. 核心概念与源码讲解

### 4.1 句柄与函数指针类型：`void*` 背后的契约

#### 4.1.1 概念说明

驱动的对外 API 几乎所有函数都以一个「句柄」打头：

- `PsiMsDaq_HandleIrq(ipHandle)` ——「整个 IP」的句柄；
- `PsiMsDaq_Str_Configure(strHndl, ...)` ——「某条流」的句柄；
- `PsiMsDaq_StrWin_GetDataUnwrapped(winInfo, ...)` ——「某个窗口」的描述符。

对外用户只能看到 `void*`，看不到字段；对内驱动把 `void*` 强转回真实结构体指针。这就是上一节讲的「不透明指针」。

和句柄配套的，还有两类「函数类型」：

- 三个内存访问函数类型（`PsiMsDaq_RegWrite_f` 等），允许把「怎么读写物理内存」的实现替换掉；
- 两个中断回调函数类型（`PsiMsDaqn_WinIrq_f` 等），允许用户把自己的处理逻辑挂进来。

此外，`PsiMsDaq_WinInfo_t` 是一个**特殊的「值类型」**：它不是句柄，而是一个把 `(窗口号, IP 句柄, 流句柄)` 打包的小结构体，按值传递、栈上构造。

#### 4.1.2 核心流程

驱动里「对外类型层」可以这样分层理解：

```
对外（.h 中可见）
├── 两个句柄 typedef:  IpHandle = void* , StrHandle = void*
├── 函数指针类型:
│     ├── RegWrite_f / RegRead_f / DataCopy_f   （内存访问）
│     └── WinIrq_f / StrIrq_f                   （中断回调）
├── 聚合 struct:  AccessFct_t  (打包三个内存访问函数)
├── 值类型 struct: WinInfo_t   (打包 winNr + 两个句柄，栈上瞬时)
└── 配置 struct:   StrConfig_t / 枚举 RetCode_t / 枚举 RecMode_t
对内（.c 中私有，下一节讲）
├── PsiMsDaq_Inst_t      （一个 IP 的全部软件状态）
└── PsiMsDaq_StrInst_t   （一条流的全部软件状态）
```

关键关系：**所有 `void*` 句柄，运行时实际指向的都是 `.c` 里的两个私有结构体。** 用户拿到的 `IpHandle` 其实就是 `PsiMsDaq_Inst_t*`，用户拿到的 `StrHandle` 其实就是 `PsiMsDaq_StrInst_t*`。

#### 4.1.3 源码精读

先看头文件里两个句柄的定义——它们就是 `void*` 的别名，没有别的：

```c
typedef void* PsiMsDaq_IpHandle;    // 整个 IP 的句柄
typedef void* PsiMsDaq_StrHandle;   // 某条流的句柄
```

完整定义见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.h:189-191](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L189-L191)。

再看三个内存访问的「函数类型」（注意它们定义的是**函数类型**，不是指针类型）：

```c
typedef void    PsiMsDaq_DataCopy_f(void* dst, void* src, size_t n);
typedef void    PsiMsDaq_RegWrite_f (const uint32_t addr, const uint32_t value);
typedef uint32_t PsiMsDaq_RegRead_f (const uint32_t addr);
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.h:201](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L201)、[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:209](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L209)、[drivers/psi_ms_daq_axi/src/psi_ms_daq.h:217](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L217)。

三个函数类型用 `PsiMsDaq_AccessFct_t` 打包成一个结构体，这样 `PsiMsDaq_Init` 只需要接收一个指针就能同时注入三个实现：

```c
typedef struct {
    PsiMsDaq_DataCopy_f* dataCopy;
    PsiMsDaq_RegWrite_f* regWrite;
    PsiMsDaq_RegRead_f*  regRead;
} PsiMsDaq_AccessFct_t;
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.h:279-283](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L279-L283)。

最后是特殊的 `PsiMsDaq_WinInfo_t`——一个值类型，把「哪个窗口、属于哪个 IP、哪条流」三件事打包：

```c
typedef struct {
    uint8_t              winNr;       // 窗口号
    PsiMsDaq_IpHandle    ipHandle;    // 该窗口所属 IP 的句柄
    PsiMsDaq_StrHandle   strHandle;   // 该窗口所属流的句柄
} PsiMsDaq_WinInfo_t;
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.h:224-228](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L224-L228)。源码注释明确强调：**「This is not a handle」**，它在栈上构造、仅在本轮函数调用内有效，**不能把它存起来跨调用使用**。它的设计意图见 4.4 节。

#### 4.1.4 代码实践

**实践目标**：亲手验证「句柄只是 `void*`」，并感受「把句柄传给错误的 API 会发生什么」（纯编译期检查）。

**操作步骤**：

1. 打开 [drivers/psi_ms_daq_axi/src/psi_ms_daq.h](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h)，确认 `PsiMsDaq_IpHandle` 和 `PsiMsDaq_StrHandle` 的 typedef 体只有一个 `void*`。
2. 写一段「示例代码」（非项目原有，仅作理解用，**不要编译进工程**）：

```c
/* 示例代码：仅用于理解句柄等价关系，不是项目源码 */
#include "psi_ms_daq.h"

void demo_types(void) {
    PsiMsDaq_IpHandle  ip  = 0;       /* 其实就是 void* ip = 0;   */
    PsiMsDaq_StrHandle str = 0;       /* 其实就是 void* str = 0;  */
    void* raw = ip;                   /* IpHandle 与 void* 互通，无需强转 */
    (void)raw; (void)str;
}
```

3. 观察第 2 步：`void* raw = ip;` 不需要任何强制类型转换就能通过——这印证了 `PsiMsDaq_IpHandle` 就是 `void*` 的别名。
4. 进一步思考：正因为两个句柄都是 `void*`，**编译器无法区分它们**。如果你不小心把 `StrHandle` 传给只接受 `IpHandle` 的函数（例如把 `str` 传给 `PsiMsDaq_RegRead`），编译器**不会报错**，只会运行时崩溃。这正是不透明指针的代价。

**需要观察的现象**：第 3 步能编译通过；第 4 步「句柄误用」也照样编译通过。

**预期结果**：句柄只是 `void*`，类型安全完全靠程序员自觉；这也是驱动 API 都要在入口做 `CheckStrNr` 等运行期校验的原因（见 u4-l4）。

> 说明：本仓库不包含可编译的驱动测试（测试在上游 `psi_multi_stream_daq`），所以本实践为**源码阅读型实践**，不需要真正编译运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么驱动要把句柄定义成 `void*` 而不是 `PsiMsDaq_Inst_t*`？

**参考答案**：因为 `PsiMsDaq_Inst_t` 定义在 `.c` 文件里、对外不可见。若头文件用 `PsiMsDaq_Inst_t*`，就必须在 `.h` 里完整暴露结构体定义，破坏了信息隐藏；用 `void*` 则用户无法直接访问字段，只能走 API，既安全又保持 ABI 稳定。

**练习 2**：`PsiMsDaq_AccessFct_t` 为什么要用 struct 把三个函数指针「打包」，而不是让 `PsiMsDaq_Init` 接收三个独立参数？

**参考答案**：打包成一个 struct 后，新增/删除一种访问函数只改 struct 定义和实现端，对外 API 签名 `PsiMsDaq_Init(..., const PsiMsDaq_AccessFct_t*)` 保持不变，二进制兼容性更好；同时调用者也可以用一个 `static const` 的结构体字面量整体传入，更清晰。

---

### 4.2 IP 实例结构体 `PsiMsDaq_Inst_t`：「一台 IP」的软件状态

#### 4.2.1 概念说明

`PsiMsDaq_Inst_t` 是「整块 IP」的软件镜像。它只活在 `.c` 文件里，用户看不到。它要记住这几件事：

- 这块 IP 挂在哪个物理地址（`baseAddr`）。
- 这块 IP 最多支持几条流、每条流最多几个窗口（`maxStreams`、`maxWindows`），这两项**必须和 Vivado 里 IP 的 GUI 设置一致**（见 u1-l4 的泛型 `Streams_g`、`MaxWindows_g`）。
- 窗口地址空间里「流间步进」有多大（`strAddrOffs`）——u3-l1 已讲过它的公式。
- 一个指向「所有流实例数组」的指针（`streams`）。
- 三个「怎么访问内存」的函数指针（`memcpyFct`、`regWrFct`、`regRdFct`）。

#### 4.2.2 核心流程

`PsiMsDaq_Inst_t` 的生命周期：

```
PsiMsDaq_Init()
   ├── malloc(PsiMsDaq_Inst_t)              → IP 实例本体（堆）
   ├── malloc(PsiMsDaq_StrInst_t * maxStreams) → 流实例数组（堆）
   ├── 填 baseAddr / maxStreams / maxWindows / strAddrOffs
   ├── 选择访问函数（默认 or 注入）
   ├── 给每条流初始化软件字段 + 写寄存器复位
   └── 返回 (PsiMsDaq_IpHandle) inst_p     → void* 指向堆上 IP 实例

之后所有 API:
   └── 把 void* 强转回 PsiMsDaq_Inst_t* 使用，直到驱动卸载（本驱动无 free，常驻）
```

字段与寄存器空间的对应关系如下表：

| 字段 | 含义 | 关联的寄存器/概念 |
| --- | --- | --- |
| `baseAddr` | IP 物理基址 | 所有 `PSI_MS_DAQ_REG_*` 宏叠加上它（u3-l1） |
| `maxStreams` | 最大流数 | 校验流号 `CheckStrNr`（[psi_ms_daq.c:79-87](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L79-L87)） |
| `maxWindows` | 每流最大窗口数 | 计算流间步进 `strAddrOffs` |
| `strAddrOffs` | 流间步进字节数 | 窗口寄存器宏的 `so` 参数（u3-l1） |
| `streams` | 流实例数组指针 | 下一节的 `PsiMsDaq_StrInst_t` |
| `memcpyFct`/`regWrFct`/`regRdFct` | 三种访问的实现 | `PsiMsDaq_RegWrite/RegRead` 调用它们（4.2.3） |

#### 4.2.3 源码精读

结构体定义（注意它在 `.c` 里，不在 `.h`）：

```c
typedef struct {
    uint32_t                  baseAddr;      // IP 物理基址
    uint8_t                   maxStreams;    // 最大流数
    uint8_t                   maxWindows;    // 每流最大窗口数
    uint32_t                  strAddrOffs;   // 窗口区的流间步进
    PsiMsDaq_StrInst_t*       streams;       // 流实例数组（堆上）
    PsiMsDaq_DataCopy_f*      memcpyFct;     // 数据拷贝实现
    PsiMsDaq_RegWrite_f*      regWrFct;      // 寄存器写实现
    PsiMsDaq_RegRead_f*       regRdFct;      // 寄存器读实现
} PsiMsDaq_Inst_t;
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:30-39](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L30-L39)。

这三个函数指针字段最直接的用法在 `PsiMsDaq_RegWrite` / `PsiMsDaq_RegRead`：它们把传入的相对地址 `addr` 叠加上 `baseAddr`，再交给函数指针去执行——这就是 u3-l1 说的「所有寄存器宏给的都是相对字节偏移」的实现处：

```c
PsiMsDaq_RetCode_t PsiMsDaq_RegWrite(PsiMsDaq_IpHandle ipHandle,
                                     const uint32_t addr, const uint32_t value) {
    PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*)ipHandle;   // void* 强转回真实类型
    inst_p->regWrFct(inst_p->baseAddr + addr, value);       // 叠加基址再调用注入实现
    return PsiMsDaq_RetCode_Success;
}
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:730-740](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L730-L740)，对应的读函数在 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:742-752](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L742-L752)。

#### 4.2.4 代码实践

**实践目标**：追踪一次寄存器读写的完整路径，看清「API → 句柄强转 → 叠加基址 → 函数指针」四步。

**操作步骤**：

1. 打开 [psi_ms_daq.c:730-740](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L730-L740) 的 `PsiMsDaq_RegWrite`。
2. 再看它依赖的两个默认实现 `PsiMsDaq_RegWrite_Standard` / `PsiMsDaq_RegRead_Standard`：

```c
void PsiMsDaq_RegWrite_Standard(const uint32_t addr, const uint32_t value) {
    volatile uint32_t* addr_p = (volatile uint32_t*)(size_t)addr;   // 把整数当地址
    *addr_p = value;                                                // 直接写物理内存
}
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:56-60](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L56-L60) 和 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:62-66](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L62-L66)。
3. 跟踪调用链：用户调用 `PsiMsDaq_Str_ClrMaxLvl(str)` → 内部 `PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_MAXLVL(strNr), 0)`（见 [psi_ms_daq.c:409-419](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L409-L419)）→ `regWrFct(baseAddr + 0x200 + 0x10*strNr, 0)` → `PsiMsDaq_RegWrite_Standard(...)` → `*((volatile uint32_t*)(baseAddr+0x200+0x10*strNr)) = 0;`。

**需要观察的现象**：相对寄存器偏移 `0x200+0x10*n` 在最后一步被叠加到 `baseAddr` 上，形成一个 32 位 volatile 写。

**预期结果**：你能复述出「相对地址 + 基址 = 物理地址」这一步发生在 `PsiMsDaq_RegWrite` 里，而真正写内存的动作发生在可被替换的 `regWrFct` 里。**待本地验证**：在真实 ZCU102 上可用逻辑分析仪/ILA 抓到这次 AXI 写事务。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `baseAddr` 写成 0，寄存器访问会怎样？

**参考答案**：`PsiMsDaq_RegWrite` 会发出 `regWrFct(0 + addr, value)`，也就是直接往地址 `0x200+0x10*n` 这种纯偏移地址写。在大多数系统里这是非法地址，会触发总线异常（data abort / segfault）；说明 `baseAddr` 必须是 IP 在地址映射表（device tree 或 xparameters）里的真实物理基址。

**练习 2**：为什么 `regWrFct` 要存成函数指针，而不是直接在 `PsiMsDaq_RegWrite` 里写死 `volatile` 指针赋值？

**参考答案**：为了让「如何访问内存」可注入。在 Zynq 裸机下默认实现（直接 volatile 指针）就够用；但在 Linux 用户态（要走 `/dev/mem` 或 UIO 的 `mmap`）、或纯软件仿真（要把读写重定向到一段普通内存数组）下，直接 volatile 访问就行不通。把访问函数做成指针，调用者只要在 `PsiMsDaq_Init` 时传一个 `PsiMsDaq_AccessFct_t`，整条驱动链路就自动切换实现，而驱动主体一行都不用改。详见 u3-l3。

---

### 4.3 流实例结构体 `PsiMsDaq_StrInst_t`：「一条流」的软件状态

#### 4.3.1 概念说明

`PsiMsDaq_StrInst_t` 是「某一条流」的软件镜像，是整个驱动里字段最多、最关键的结构体。每条流在 `PsiMsDaq_Init` 时都会在堆上分配一个实例（共 `maxStreams` 个，组成数组 `streams[]`）。

它要记住四类信息：

1. **身份**：我是第几条流（`nr`）、我配置过没有（`isConfigured`）。
2. **配置缓存**：数据宽度换算成字节（`widthBytes`）、本流实际用了几个窗口（`windows`）、缓冲起始地址（`bufStart`）、窗口大小（`winSize`）、触发后采样数（`postTrig`）。
3. **中断处理进度**：上次处理到第几个窗口（`lastProcWin`）、哪些窗口的回调已经调用过（`irqCalledWin` 位图）、用户注册的回调与参数（`irqFctWin`/`irqFctStr`/`irqArg`）。
4. **回指**：我属于哪个 IP（`ipHandle`）。

#### 4.3.2 核心流程

流实例里的字段在两条路径上被读写：

```
【配置路径】PsiMsDaq_Str_Configure(strHndl, cfg)
   ├── 校验 streamWidthBits % 8 == 0
   ├── 校验 winCnt <= maxWindows、winSize 对齐
   ├── 把 cfg.* 写进寄存器（POSTTRIG/MODE/SCFG/BUFSTART/WINSIZE）
   └── 把 cfg.* 缓存进 str_p->widthBytes / windows / bufStart / postTrig / winSize

【中断路径】PsiMsDaq_HandleIrq(ipHandle)
   ├── 对每条有中断的流，循环推进 lastProcWin → lastWrittenWin 之间的新窗口
   ├── 用 irqCalledWin 位图标记「这个窗口回调已调用过，别重复」
   └── 构造 WinInfo_t 调用 irqFctWin

【释放路径】PsiMsDaq_StrWin_MarkAsFree(winInfo)
   └── 清除 irqCalledWin 中对应窗口的位，允许该窗口再次产生回调
```

字段速查表：

| 字段 | 类型 | 作用 | 何时写入 |
| --- | --- | --- | --- |
| `nr` | `uint8_t` | 流号 | `Init` 时 `= str` |
| `isConfigured` | `bool` | 是否已配置 | `Str_Configure` 末尾置 `true` |
| `widthBytes` | `uint8_t` | 采样宽度（字节） | `Str_Configure`，`= widthBits/8` |
| `windows` | `uint8_t` | 本流实际窗口数 | `Str_Configure`，`= winCnt` |
| `lastProcWin` | `int8_t` | 上次处理的窗口号，初值 -1 | `HandleIrq` 每处理一个窗口更新 |
| `irqCalledWin` | `uint32_t` | 窗口回调已调位图（位 = 窗口号） | `HandleIrq` 置位 / `MarkAsFree` 清位 |
| `irqFctWin` | 函数指针 | 窗口式回调 | `SetIrqCallbackWin` |
| `irqFctStr` | 函数指针 | 流式回调 | `SetIrqCallbackStr` |
| `irqArg` | `void*` | 回调用户参数 | 上面两个注册函数 |
| `ipHandle` | `void*` | **回指所属 IP 实例** | `Init` 时 `= inst_p` |
| `bufStart` | `uint32_t` | 缓冲起始地址 | `Str_Configure` |
| `winSize` | `uint32_t` | 窗口大小（字节） | `Str_Configure` |
| `postTrig` | `uint32_t` | 触发后采样数（含触发点） | `Str_Configure` |

> 注意 `windows`（本流实际窗口数，由配置决定）与上一节 `maxWindows`（IP 级上限，由硬件泛型决定）的区别：前者 ≤ 后者。

#### 4.3.3 源码精读

结构体定义：

```c
typedef struct {
    uint8_t                 nr;            // 流号
    bool                    isConfigured;  // 是否已配置
    uint8_t                 widthBytes;    // 采样宽度（字节）
    uint8_t                 windows;       // 本流窗口数
    int8_t                  lastProcWin;   // 上次处理的窗口号（-1 表示无）
    uint32_t                irqCalledWin;  // 「回调已调用」位图
    PsiMsDaqn_WinIrq_f*     irqFctWin;     // 窗口式 IRQ 回调
    PsiMsDaqn_StrIrq_f*     irqFctStr;     // 流式 IRQ 回调
    void*                   irqArg;        // 回调用户参数
    PsiMsDaq_IpHandle       ipHandle;      // 回指所属 IP 实例
    uint32_t                bufStart;      // 缓冲起始地址
    uint32_t                winSize;       // 窗口大小
    uint32_t                postTrig;      // 触发后采样数
} PsiMsDaq_StrInst_t;
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:13-27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L13-L27)。

回指指针 `ipHandle` 的用法是理解整个驱动的钥匙。几乎所有「流级 API」只接收一个 `PsiMsDaq_StrHandle`，但它们要写寄存器时，需要的是 IP 级的 `ipHandle`（因为寄存器读写函数要拿到 `baseAddr` 和访问函数指针）。于是驱动这样做：

```c
PsiMsDaq_RetCode_t PsiMsDaq_Str_Configure(PsiMsDaq_StrHandle strHndl, ...) {
    PsiMsDaq_StrInst_t* inst_p = (PsiMsDaq_StrInst_t*) strHndl;   // 流句柄强转
    PsiMsDaq_IpHandle   ipHandle = inst_p->ipHandle;               // ★ 取出回指
    PsiMsDaq_Inst_t*    ipInst_p = (PsiMsDaq_Inst_t*) ipHandle;    // 再转回 IP 实例
    ...
    SAFE_CALL(PsiMsDaq_RegWrite(ipHandle, PSI_MS_DAQ_REG_POSTTRIG(strNr), ...));
}
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:264-271](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L264-L271)。这短短两行 `inst_p->ipHandle` 读取，是「从流回到 IP」的唯一桥梁——没有它，流级函数就够不到 `baseAddr` 和访问函数。

`irqCalledWin` 位图的用法：中断处理时，对一个还没被用户释放的窗口，置上对应位，下次再来中断就跳过它，避免重复回调（详见 u4-l2）。置位见 [psi_ms_daq.c:242](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L242)（`str_p->irqCalledWin |= (1 << win);`），清位见 `PsiMsDaq_StrWin_MarkAsFree` 里 `str_p->irqCalledWin &= ~(1 << winInfo.winNr);`，在 [psi_ms_daq.c:641-653](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L641-L653)。

#### 4.3.4 代码实践

**实践目标**：理解「配置路径」如何同时写硬件寄存器和软件缓存——这是驱动区别于「直接读写寄存器」的关键。

**操作步骤**：

1. 打开 [psi_ms_daq.c:264-320](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L264-L320) 的 `PsiMsDaq_Str_Configure`。
2. 把函数体分成两段读：前半段（`PsiMsDaq_RegWrite`/`RegSetField`/`RegSetBit` 一系列调用）是「写硬件寄存器」，后半段（`inst_p->widthBytes = ...` 等）是「写软件缓存」。
3. 对照看：`winSize` 这个值被写了两遍——一次写进硬件寄存器 `PSI_MS_DAQ_CTX_WINSIZE(strNr)`，一次缓存进 `inst_p->winSize`。后续 `GetDataUnwrapped` 计算 `winStart = str_p->bufStart + str_p->winSize*winNr`（[psi_ms_daq.c:607](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L607)）时，用的就是缓存值，而不是再去读寄存器。

**需要观察的现象**：配置函数「双写」——硬件寄存器和软件缓存各一份。

**预期结果**：理解软件缓存的意义：读取数据时要用 `bufStart`/`winSize`/`postTrig` 算地址，而这些值在硬件寄存器里**不是只读就能拿到的稳定字段**（例如 `postTrig` 还要和窗口实际采样数做减法才能得到 preTrig），所以驱动选择在配置时把它们缓存下来，后续直接用，避免重复换算和重复读寄存器。

#### 4.3.5 小练习与答案

**练习 1**：`lastProcWin` 为什么是 `int8_t` 且初值 `-1`，而不是 `uint8_t` 初值 `0`？

**参考答案**：因为「第 0 个窗口」是一个合法的、可能需要处理的窗口号。如果初值是 0，驱动就分不清「第 0 个窗口处理过了」和「还没开始处理」。用 `-1` 作哨兵值（sentinel）表示「一个窗口都没处理过」，第一次推进时 `(win+1) % windows` 会从 0 开始（见 [psi_ms_daq.c:231-237](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L231-L237)），逻辑正确。

**练习 2**：为什么「窗口式回调」和「流式回调」不能同时用（见 `SetIrqCallbackWin`/`SetIrqCallbackStr` 的互斥校验）？

**参考答案**：因为两种方案对中断的「消费粒度」不同——窗口式由驱动代为逐个窗口调回调，流式只把中断事件转发给用户。若两者同时注册，同一个中断会被处理两遍、职责重叠。所以 [psi_ms_daq.c:343-345](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L343-L345) 在注册窗口式回调前先检查 `irqFctStr != NULL`，反之亦然，违反时返回 `IrqSchemesWinAndStrAreExclusive`。详见 u4-l2。

---

### 4.4 句柄如何连接：`PsiMsDaq_Init`、`PsiMsDaq_GetStrHandle` 与回指指针

#### 4.4.1 概念说明

前两节定义了两个私有结构体，但它们怎么被「创造出来」「互相连接」「对外暴露成句柄」？这一节看 `PsiMsDaq_Init` 和 `PsiMsDaq_GetStrHandle` 这两个构造函数。

核心设计是**双向引用**：

- IP 实例 → 流实例：`PsiMsDaq_Inst_t.streams` 指向流实例数组。
- 流实例 → IP 实例：`PsiMsDaq_StrInst_t.ipHandle` 回指 IP 实例。

这样无论你手里拿的是 IP 句柄还是流句柄，都能在两个结构体之间自由穿梭。

此外，`PsiMsDaq_WinInfo_t` 这个栈上值类型，是给「窗口级 API」用的临时通行证——它按值携带 `(winNr, ipHandle, strHandle)`，函数返回后就失效，所以**它本身不持有任何所有权**，只是三个值的快照。

#### 4.4.2 核心流程

句柄诞生与互连的全过程：

```
PsiMsDaq_Init(baseAddr, maxStreams, maxWindows, accessFct_p)
  ① malloc 一个 IP 实例          inst_p
  ② malloc 流实例数组            inst_p->streams[]
  ③ 填 IP 级字段                 baseAddr / maxStreams / maxWindows / strAddrOffs
  ④ 选访问函数                   accessFct_p==NULL ? 默认函数 : 注入函数
  ⑤ 复位全部流/IRQ 寄存器        GCFG/STRENA/IRQENA/IRQVEC
  ⑥ for 每条流:
        复位每流硬件（MAXLVL、各窗口 WINCNT=0）
        初始化流软件字段:
            streams[str].nr        = str
            streams[str].ipHandle  = inst_p   ← 建立回指！
            streams[str].lastProcWin = -1
            ...回调清空...
  ⑦ 置总使能 GCFG = ENA | IRQENA
  ⑧ return (PsiMsDaq_IpHandle) inst_p        ← 对外变成 void*

PsiMsDaq_GetStrHandle(ipHandle, streamNr, &strHndl)
  ① 把 ipHandle 强转回 PsiMsDaq_Inst_t*
  ② 校验 streamNr < maxStreams
  ③ *strHndl = (PsiMsDaq_StrHandle) &inst_p->streams[streamNr]
                                        ↑ 取数组元素地址，对外变成 void*
```

#### 4.4.3 源码精读

`PsiMsDaq_Init` 的内存分配与回指建立（节选关键行）：

```c
PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*) malloc(sizeof(PsiMsDaq_Inst_t));
inst_p->baseAddr   = baseAddr;
inst_p->streams    = (PsiMsDaq_StrInst_t*) malloc(sizeof(PsiMsDaq_StrInst_t)*maxStreams);
inst_p->maxWindows = maxWindows;
inst_p->maxStreams = maxStreams;
inst_p->strAddrOffs= Pow(2, Log2Ceil(maxWindows))*0x10;
...
for (int str = 0; str < maxStreams; str++) {
    ...
    inst_p->streams[str].nr          = str;
    inst_p->streams[str].isConfigured= false;
    inst_p->streams[str].ipHandle    = (PsiMsDaq_IpHandle) inst_p;   // ★ 回指
    inst_p->streams[str].lastProcWin = -1;
    inst_p->streams[str].irqCalledWin= 0;
}
...
return (PsiMsDaq_IpHandle) inst_p;   // ★ 对外变 void*
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:132-181](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L132-L181)（回指在第 [174](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L174) 行，返回句柄在第 [180](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L180) 行）。

`PsiMsDaq_GetStrHandle` 只有三步：强转、校验、取数组元素地址：

```c
PsiMsDaq_RetCode_t PsiMsDaq_GetStrHandle(PsiMsDaq_IpHandle ipHandle,
                                         const uint8_t streamNr,
                                         PsiMsDaq_StrHandle* const strHndl_p) {
    PsiMsDaq_Inst_t* inst_p = (PsiMsDaq_Inst_t*) ipHandle;
    SAFE_CALL(CheckStrNr(ipHandle, streamNr));                     // 校验流号
    *strHndl_p = (PsiMsDaq_StrHandle) &inst_p->streams[streamNr];  // ★ 取元素地址
    return PsiMsDaq_RetCode_Success;
}
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:183-195](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L183-L195)。注意它**没有再 malloc**——流句柄只是「指向 IP 实例内部数组某个元素的指针」，内存早在 `Init` 一次性分配好了。这就是为什么 `GetStrHandle` 几乎零开销、可以随时调用。

栈上 `WinInfo_t` 的构造发生在中断处理里（窗口式回调分发时）：

```c
PsiMsDaq_WinInfo_t winInfo;
winInfo.ipHandle  = ipHandle;
winInfo.strHandle = strHandle;
winInfo.winNr     = win;
if (str_p->irqFctWin != NULL) {
    str_p->irqFctWin(winInfo, str_p->irqArg);   // 按值传递整个 struct
}
```

见 [drivers/psi_ms_daq_axi/src/psi_ms_daq.c:244-250](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L244-L250)。`winInfo` 是 `do…while` 循环体里的局部变量，每次迭代重建、按值传给回调，循环一出作用域即销毁——典型的「栈上瞬时」用法，所以头文件注释才特意警告「不是句柄、函数返回即失效」。

#### 4.4.4 代码实践

**实践目标**：画出一次 `PsiMsDaq_GetStrHandle(ip, 2, &str)` 调用后，`ip` 句柄、`str` 句柄、流实例的回指指针三者指向的内存关系，并解释 `str->ipHandle` 回指指针的作用。这是本讲的总练习。

**操作步骤**：

1. 假设执行过：

```c
/* 示例代码 */
PsiMsDaq_IpHandle  ip;
PsiMsDaq_StrHandle str;
ip = PsiMsDaq_Init(0x40000000, 4, 8, NULL);   // baseAddr=0x40000000, 4 流, 8 窗
PsiMsDaq_GetStrHandle(ip, 2, &str);
```

2. 根据 `PsiMsDaq_Init`（[psi_ms_daq.c:132-181](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L132-L181)）和 `PsiMsDaq_GetStrHandle`（[psi_ms_daq.c:183-195](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L183-L195)）推导出下面的内存图。
3. 用文字回答：`str->ipHandle` 指向哪里？为什么流级函数非靠它不可？

**推导出的内存关系图**（手画即可，下面是参考答案）：

```
   ip (void*)                      str (void*)
     │                                │
     ▼                                ▼
┌─────────────────────┐      ┌──────────────────────┐
│ PsiMsDaq_Inst_t     │      │ PsiMsDaq_StrInst_t   │  ← streams[2]
│  (堆, malloc#1)     │      │  (堆数组, malloc#2)   │
│                     │      │                      │
│  baseAddr=0x40000000│      │  nr = 2              │
│  maxStreams = 4     │      │  isConfigured=false  │
│  maxWindows = 8     │      │  lastProcWin = -1    │
│  strAddrOffs=0x80   │      │  irqCalledWin=0      │
│                     │      │                      │
│  streams ───────────┼──►───┼──► [0],[1],[2],[3]   │
│  regWrFct, ...      │      │                      │
│                     │  ┌───┼── ipHandle ──────────┼──┐
└─────────────────────┘  │   └──────────────────────┘  │
                         │                             │
                         └─────────────────────────────┘
                          回指：从流实例指回 IP 实例
```

要点：

- `ip` 指向堆上的 `PsiMsDaq_Inst_t`（第一次 `malloc`）。
- `inst_p->streams` 指向堆上的流实例数组（第二次 `malloc`，共 4 个元素）。
- `str` 不是新分配的内存，而是 `&streams[2]`，即数组第 3 个元素的地址。
- `streams[2].ipHandle` 又指回 `PsiMsDaq_Inst_t`，形成 **IP → 流 → IP 的双向引用**。

**`str->ipHandle` 回指指针的作用**：流级 API（如 `PsiMsDaq_Str_Configure`、`Str_SetEnable`、`Str_Arm`）只接收一个 `PsiMsDaq_StrHandle`。它们要写寄存器时，必须调用 `PsiMsDaq_RegWrite(ipHandle, ...)`，而该函数需要 `baseAddr` 与访问函数指针——这两者存在 IP 实例里。回指指针就是「从流回到 IP」的唯一通道：`PsiMsDaq_IpHandle ipHandle = inst_p->ipHandle;`（[psi_ms_daq.c:269](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L269)）。没有它，流级函数就完全够不到 IP 级状态，整个驱动就散架了。

**预期结果**：你能画出双向引用图，并说清楚「流→IP 的回指是为了拿到 baseAddr 与访问函数」。

#### 4.4.5 小练习与答案

**练习 1**：`PsiMsDaq_GetStrHandle` 调用 `malloc` 了吗？为什么可以这样设计？

**参考答案**：没有。因为 `PsiMsDaq_Init` 已经一次性为所有 `maxStreams` 条流 `malloc` 好了整个 `streams` 数组（[psi_ms_daq.c:140](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L140)）。`GetStrHandle` 只是返回 `&streams[streamNr]`，即「数组里已存在元素的地址」，所以零分配、可随时反复调用，也不会产生内存碎片。

**练习 2**：`PsiMsDaq_WinInfo_t` 为什么设计成「按值传递的栈变量」，而不是像 `StrHandle` 那样设计成「指向某个长期对象的指针」？

**参考答案**：因为窗口的「身份」(winNr) 在不同中断里会变，而且窗口信息只在那一次回调里有意义；做成值类型按值传递，调用者无需管理它的生命周期、也不会悬空。如果设计成指针，就得为每个窗口额外维护一个长期对象并管理其释放，反而复杂。代价是它**不能被回调存起来以后再用**——这正是头文件注释反复强调的点（[psi_ms_daq.h:220-223](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L220-L223)）：要在回调里长期记住某窗口，只能记住 `(winNr, strHandle)` 这两个值，下次再重新组装 `WinInfo_t` 调用窗口函数。

---

## 5. 综合实践

把本讲的「句柄」「双向引用」「软件缓存」串起来，做一次端到端的心智追踪。

**任务背景**：参考 u3-l1 的寄存器布局和本讲的数据模型。假设执行下面的「示例代码」：

```c
/* 示例代码：仅用于追踪数据模型，不是项目原有源码 */
PsiMsDaq_IpHandle  ip  = PsiMsDaq_Init(0x40000000, 2, 8, NULL);
PsiMsDaq_StrHandle s0, s1;
PsiMsDaq_GetStrHandle(ip, 0, &s0);
PsiMsDaq_GetStrHandle(ip, 1, &s1);

PsiMsDaq_StrConfig_t cfg = {
    .postTrigSamples = 5,
    .recMode         = PsiMsDaqn_RecMode_TriggerMask,
    .winAsRingbuf    = true,
    .winOverwrite    = false,
    .winCnt          = 4,
    .bufStartAddr    = 0x40000000,
    .winSize         = 32,
    .streamWidthBits = 16,
};
PsiMsDaq_Str_Configure(s0, &cfg);
```

请完成以下三件事（纸笔即可）：

1. **画内存关系图**：画出 `ip`、`s0`、`s1` 三个句柄各自指向哪里、`streams` 数组有几个元素、每条流的 `ipHandle` 回指到哪。
2. **追软件缓存**：`PsiMsDaq_Str_Configure(s0, &cfg)` 执行完后，`streams[0]` 这个流实例的 `widthBytes`、`windows`、`bufStart`、`winSize`、`postTrig`、`isConfigured` 分别变成什么值？（参考 [psi_ms_daq.c:311-317](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L311-L317)）
3. **追地址生成**：之后若调用窗口函数读 `winNr=2` 的数据，`GetDataUnwrapped` 内部计算 `winStart = str_p->bufStart + str_p->winSize*winInfo.winNr`（[psi_ms_daq.c:607](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L607)）会得到哪个地址？它用的是寄存器里的值还是软件缓存里的值？

**参考要点**：

1. `ip` 指向堆上 `PsiMsDaq_Inst_t`（`maxStreams=2`、`maxWindows=8`、`strAddrOffs = 2^3*0x10 = 0x80`）；`streams` 数组有 2 个元素；`s0 = &streams[0]`、`s1 = &streams[1]`；两条流的 `ipHandle` 都回指 `ip`。
2. `widthBytes = 16/8 = 2`；`windows = 4`；`bufStart = 0x40000000`；`winSize = 32`；`postTrig = 5`；`isConfigured = true`。
3. `winStart = 0x40000000 + 32*2 = 0x40000040`。用的是**软件缓存**（`str_p->winSize`、`str_p->bufStart`），这正是 `Str_Configure` 末尾要缓存这些字段的原因。

> 说明：本仓库不含可编译的驱动测试，本综合实践为**源码阅读型实践**，无需运行；待本地验证可在上游 `psi_multi_stream_daq` 的测试平台或 ZCU102 参考设计（u5-l2）中实操。

## 6. 本讲小结

- 两个句柄 `PsiMsDaq_IpHandle`、`PsiMsDaq_StrHandle` 本质都是 `void*`，是为了信息隐藏——真实结构体定义藏在 `.c` 里（[psi_ms_daq.h:189-191](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L189-L191)）。
- IP 实例 `PsiMsDaq_Inst_t` 记住「一台 IP」的状态：`baseAddr`、`maxStreams`、`maxWindows`、`strAddrOffs`、流实例数组指针、三个访问函数指针（[psi_ms_daq.c:30-39](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L30-L39)）。
- 流实例 `PsiMsDaq_StrInst_t` 记住「一条流」的软件状态：身份（`nr`/`isConfigured`）、配置缓存（`widthBytes`/`windows`/`bufStart`/`winSize`/`postTrig`）、中断进度（`lastProcWin`/`irqCalledWin`）、回调（`irqFctWin`/`irqFctStr`/`irqArg`）和回指（`ipHandle`）（[psi_ms_daq.c:13-27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L13-L27)）。
- 内存访问做成可注入函数指针（`AccessFct_t`），让同一份驱动能跑在裸机、Linux、仿真等不同环境，只需在 `PsiMsDaq_Init` 时换一组实现（[psi_ms_daq.c:145-154](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L145-L154)）。
- `Init` 在堆上一次性分配 IP 实例和流实例数组，并建立 **IP→流（`streams`）与流→IP（`ipHandle`）的双向引用**；`GetStrHandle` 零分配，只返回数组元素地址（[psi_ms_daq.c:132-195](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.c#L132-L195)）。
- `PsiMsDaq_WinInfo_t` 是「按值传递、栈上瞬时」的值类型，不是句柄、不可长期持有，是窗口级 API 的临时通行证（[psi_ms_daq.h:224-228](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/drivers/psi_ms_daq_axi/src/psi_ms_daq.h#L224-L228)）。

## 7. 下一步学习建议

本讲把「数据结构」和「句柄」讲清了，但 `PsiMsDaq_Init` 里那段复位序列、`strAddrOffs = Pow(2,Log2Ceil(maxWindows))*0x10` 的计算、以及三个默认访问函数的 `volatile` 指针写法，我们还只点到为止。下一讲 **u3-l3「初始化与寄存器访问抽象」** 会专门精读：

- `PsiMsDaq_Init` 的完整复位序列与末尾为何要置 `GCFG_BIT_ENA | IRQENA`。
- `Log2`/`Log2Ceil`/`Pow` 三个辅助函数如何算出 `strAddrOffs`。
- 默认访问函数 `PsiMsDaq_RegWrite_Standard` 等为何能在 Zynq 裸机直接读写物理地址，以及如何在 Linux 用户态注入自定义访问函数。

之后再进入 u3-l4「流配置流程」看 `PsiMsDaq_Str_Configure` 如何把 `StrConfig_t` 翻译成一串寄存器写入与软件缓存更新（本讲已埋下伏笔）。建议在继续前，先把本讲 4.4 节的内存关系图自己画一遍，确保「双向引用 + 软件缓存」的模型已经牢固。
