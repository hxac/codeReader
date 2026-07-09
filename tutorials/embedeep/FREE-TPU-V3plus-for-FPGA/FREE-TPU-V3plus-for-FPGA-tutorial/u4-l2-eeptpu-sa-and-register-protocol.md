# EEPTPU_SA 类与 TPU 寄存器协议

## 1. 本讲目标

本讲是裸机（standalone）路线的核心一讲。在上一讲 u4-l1 里，我们看清了 standalone 工程的整体结构和 `main.cc` 的菜单流程，也知道了裸机侧不像 Linux 路线那样调用高层 `EEPTPU` 类，而是**直接读写 TPU 的控制寄存器**来驱动一次推理。本讲就要把这套「寄存器协议」彻底讲透。

学完本讲，你应当能够：

- 说清 `EEPTPU_SA` 这个裸机版 TPU 对象**有哪些核心成员与方法**，以及它和底层 `EEP_INTERFACE` 的分工。
- 背出 `config.h` 里那一组 `EEPTPU_BASEADDR0~3 / ALGOADDR / STARTUP / STATUS / RUNTIMER` 寄存器**各自的偏移地址和用途**。
- 一步一步复述 `tpu_forward` 的**「写基地址 → 写算法地址 → 写启动字 → 轮询状态位」**完整时序。
- 知道当前工程里**存在两套等价的 forward 实现**（`EEPTPU_SA::forward()` 与 `main.cc` 里的 `tpu_forward()`），并理解为什么。

本讲只讲「如何启动一次推理并在寄存器层面判定它完成」；至于推理完成后输出张量在内存里是什么布局、怎么读出来还原成 `ncnn::Mat`，那是下一单元 U5（u5-l2 输出读取）的内容；至于底层 `EEP_INTERFACE` 的 `mem_read/write`、地址对齐等细节，则是后续 u4-l3 的内容。本讲只在必要处点到为止。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，什么是「寄存器协议」。** 在 ZynqMP 这颗 SoC 里，TPU IP 跑在 FPGA 可编程逻辑（PL）上，ARM 处理器（PS）要指挥它干活，靠的是两条 AXI 总线（这一点 u1-l3、u1-l4 已建立）：一条**控制通路**把 TPU 的一小块寄存器映射到 ARM 的物理地址空间（`0xA0000000` 起），一条**数据通路**让 TPU 能访问 DDR 里的张量（`0x31000000` 起）。所谓「协议」，就是 ARM 按照约定的顺序，往控制通路上那几个寄存器里写特定的值，TPU 就开始算；算完之后，TPU 会把某个状态寄存器的某一位置 1，ARM 轮询到这一位就知道「完成了」。这跟你在单片机上「写一个 GPIO 启动外设、再读一个标志位等它忙完」是同一个套路。

**第二，为什么有 `EEPTPU_SA` 这个类。** Linux 路线用的是闭源运行库 `libeeptpu_pub` 提供的 `EEPTPU` 类，调用 `forward()` 就完事，细节藏在 `.so` 里。裸机没有这个库，也没有操作系统，于是项目用 C++ 自己写了一个对等物——`EEPTPU_SA`（SA = Standalone）。它把「解析配置、加载权重、写寄存器启动、轮询完成、读输出」这些步骤都封装成方法，让 `main.cc` 调用起来更整洁。它的本质，就是**一组对寄存器协议的 C++ 封装**。

**第三，两类寄存器要分清。** TPU 的控制寄存器块里有两类寄存器：一类是**配置类**（BASEADDR0~3、ALGOADDR），告诉 TPU「权重在哪、输入在哪、临时缓冲在哪、输出写到哪、算法调度表从哪开始」；另一类是**控制/状态类**（STARTUP、STATUS、RUNTIMER），负责「启动」和「报告完成与计时」。本讲的 forward 协议，就是「先填满配置类，再触发控制类，最后读状态类」。

> 小贴士：裸机代码里随处可见 `Xil_DCacheFlush()`，是因为裸机没有操作系统代管缓存，ARM 的数据缓存（D-Cache）里可能还有没写回 DDR 的数据，而 TPU 是直接从 DDR 取数的——两者必须手动同步。这一点 u4-l1 已讲过，本讲在 forward 相关处会再提醒一次。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [sdk/standalone/src/eeptpu/eeptpu_sa.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h) | `EEPTPU_SA` 类与 `st_hwaddr_info` 结构声明 | 类的成员与方法清单 |
| [sdk/standalone/src/eeptpu/eeptpu_sa.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp) | `EEPTPU_SA` 方法实现 | `eeptpu_init`、`eep_tpu_start_work`、`eep_tpu_wait_done`、`forward` |
| [sdk/standalone/src/config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h) | 全局编译开关与 TPU 寄存器宏定义 | 一组寄存器偏移宏与基地址常量 |
| [sdk/standalone/src/main.cc](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc) | 裸机主程序 | 实际被调用的 `tpu_forward()` 函数与初始化序列 |
| sdk/standalone/src/eeptpu/interface/eep_interface.h / .cpp | 底层 AXI 读写抽象 | `register_wait` 的轮询实现（本讲点至为止，详讲见 u4-l3） |

记忆口诀：**`config.h` 定义「寄存器在哪」，`eeptpu_sa.*` 定义「类怎么封装协议」，`main.cc` 演示「协议实际怎么跑」**。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①`EEPTPU_SA` 类结构；②TPU 寄存器定义；③forward 寄存器时序；④完成状态轮询。它们恰好对应一次推理从「准备」到「启动」到「等完成」的全过程。

### 4.1 EEPTPU_SA 类结构

#### 4.1.1 概念说明

`EEPTPU_SA` 是裸机版的 TPU 对象，可以理解成「一个会自己驱动 TPU 寄存器的小管家」。它内部保存了一次推理需要的所有上下文：

- **地址上下文**：权重/输入/临时/输出四段内存的基址（`hwbase0~3`）、算法调度表地址（`addr_alg`）、寄存器块基址（`tpureg_addr`）、张量数据区基址（`mem_base`）。
- **形状上下文**：输出张量的地址与 NCHW 形状（`addr_out`，是一个 `vector`，因为检测类网络有多个输出分支）、输入张量的地址与形状（`addr_in`）。
- **预处理上下文**：`mean`、`norm` 两个浮点列表，供输入预处理使用。
- **格式上下文**：`bin_type`（`enc=1` 还是 `pub=2`）、`mem_cnt`（内存段数）。
- **底层接口对象**：`eepif`，一个 `EEP_INTERFACE` 实例，所有真正的寄存器/内存读写都委托给它。

之所以把状态都放进一个类，是因为裸机环境下没有全局运行时帮你管理「这个网络加载到哪了、输入要写到哪个地址」，必须由对象自己记住。`main.cc` 里就声明了一个全局实例 `EEPTPU_SA eepsa;`，整个程序都围着它转。

#### 4.1.2 核心流程

一个 `EEPTPU_SA` 对象的生命周期是：

```text
构造(EEPTPU_SA eepsa)
   │  成员清零，bin_type 默认置 1
   ▼
eeptpu_init(...)            ← 解析 eepnet_config 数组，填好所有地址/形状/mean/norm
   │  并把 eepif 的 mem_base_addr / tpu_reg_base 设好
   ▼
（外部把权重 mem、输入数据写进对应 DDR 地址）
   ▼
forward()                   ← 内部 = eep_tpu_start_work(addr_alg) + eep_tpu_wait_done()
   │                          写基地址→写算法地址→写启动字→轮询状态位
   ▼
read_forward_result(...)    ← 从输出地址把结果读出来（U5 详讲）
   ▼
eeptpu_deinit()             ← 当前实现为空，占位
```

注意：`forward()` 方法把「启动 + 等完成」两步打包，但 `main.cc` 实际并没有调用 `eepsa.forward()`，而是自己写了一个等价的 `tpu_forward()`（见 4.3）。这两套实现做的事完全一样，本讲会把它们对照讲清楚。

#### 4.1.3 源码精读

**类声明**集中在一个头文件里。先看类的方法清单（构造、初始化、寄存器封装、forward）：

[eeptpu_sa.h:37-60](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L37-L60) —— `EEPTPU_SA` 类的 public 方法声明。可以看到方法分四组：
- 加载类：`mem_load` / `mem_load_from_ram`（把权重字节流写到 DDR）。
- 生命周期：两个重载的 `eeptpu_init`、`eeptpu_deinit`、`eeptpu_input`。
- **寄存器封装（本讲主角）**：`eepreg_read` / `eepreg_write` / `eepreg_wait`，以及更高层的 `tpu_read_hw_ver`、`eep_tpu_read_hw_config`、`eep_tpu_start_work`、`eep_tpu_wait_done`。
- 推理：`forward`、`read_forward_result`。

**类的成员变量**记录了全部上下文：

[eeptpu_sa.h:62-94](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L62-L94) —— 关键成员：`eepif`（底层接口对象）、`mem_base`、`hwbase0~3`、`memsize`、`tpureg_addr`、`addr_out`（输出地址+形状列表）、`addr_alg`（算法地址）、`addr_in`（输入地址+形状）、`mean`/`norm`、`bin_type`、`mem_cnt`。其中 `hwbase0~3` 就是稍后要写进 `BASEADDR0~3` 寄存器的四个值。

**输出/输入形状**用一个公共结构体描述：

[eeptpu_sa.h:30-35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L30-L35) —— `st_hwaddr_info`：`hwaddr`（DDR 绝对地址）、`shape[4]`（NCHW）、`exp`（定点指数，用于反量化）。`addr_out` 是这个结构的 `vector`，`addr_in` 是单个实例。

**构造函数**把成员清零、给 `bin_type` 一个默认值：

[eeptpu_sa.cpp:29-47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L29-L47) —— 注意第 46 行 `bin_type = 1; // enc=1; pub=2`，这是默认值；真实值随后由 `eeptpu_init` 从 `eepnet_config` 数组覆盖。

**`eeptpu_init`** 是最关键的一段：它解析 u3-l3 讲过的那段 160 字节配置数组，把 `hwbase0~3`、`addr_alg`、`addr_out`、`addr_in`、`mean`、`norm` 全部填好，并完成两件与本讲强相关的事——

[eeptpu_sa.cpp:122-125](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L122-L125) —— 裸机模式忽略配置数组里的 interface/mem_base/tpureg_addr/reg_size 四个字，**改用 `config.h` 里的宏硬编码地址**：`mem_base = EEPTPU_MEM_BASE_ADDR`（0x31000000）、`tpureg_addr = EEPTPU_REG_BASE_ADDR`（0xA0000000）。这就是 u1-l4 反复强调的「魔法地址由硬件设计定死」在软件侧的落脚点。

[eeptpu_sa.cpp:127-140](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L127-L140) —— 读 `bin_type` 后按类型取基址：`enc=1` 只读 2 个基址（`hwbase0/1`），`pub=2` 读 4 个基址（`hwbase0~3`）。每个偏移 `ofs` 都要加上 `mem_base` 才是绝对地址（支持重定位）。

[eeptpu_sa.cpp:186-191](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L186-L191) —— 把 `mem_base`、`tpureg_addr` 同步到底层接口对象 `eepif`（这一步之后，`eepif` 才知道寄存器块和数据区各在哪），并读一次偏移 `0x44` 的硬件版本寄存器打印出来。

**寄存器读/写/等待的封装**只有薄薄一层，关键是「偏移 `|` 上基址」：

[eeptpu_sa.cpp:238-253](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L238-L253) —— `eepreg_read/write/wait` 都把传入的「寄存器偏移」与 `tpureg_addr` 做**按位或**（`tpureg_addr | regaddr`），再交给 `eepif`。因为 `tpureg_addr=0xA0000000` 的低 16 位全是 0，而偏移最大也就 `0x5C`，按位或等价于「基址 + 偏移」，但写起来更短。这正是后面 `eep_tpu_start_work` 里那些 `0x00000050`、`0x00000034` 数字的含义——它们都是**偏移量**。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：建立「类成员 → 寄存器值」的对应关系。

**操作步骤**：

1. 打开 `eeptpu_sa.h`，在 `EEPTPU_SA` 类里找到 `hwbase0`、`hwbase1`、`hwbase2`、`hwbase3`、`addr_alg`、`bin_type` 这六个成员。
2. 打开 `eeptpu_sa.cpp` 的 `eeptpu_init`，确认这六个成员分别是从配置数组的哪个字段读出来的（提示：`hwbase0~3` 来自 `base_ofs`，`addr_alg` 来自 `DataAlg_addr`，`bin_type` 是分支开关）。
3. 打开 `main.cc:297-304`，看 `main` 怎么把这六个成员拷给全局变量：`waddr=eepsa.hwbase0; sd_input_addr=eepsa.hwbase1; tpu_hwbase2=eepsa.hwbase2; tpu_hwbase3=eepsa.hwbase3; tpu_algbase=eepsa.addr_alg;`。

**需要观察的现象 / 预期结果**：你会发现 `main.cc` 并没有直接用 `eepsa.hwbase0` 去驱动寄存器，而是先拷贝到 `waddr` 等五个全局变量，再由 `tpu_forward()` 使用这些全局变量。这是工程的一种写法风格——把类成员「摊平」成全局量，方便 `tpu_forward()` 用宏直接写寄存器。能否跑通与硬件相关，结论属于「源码阅读型实践」，无需运行即可确认对应关系。

#### 4.1.5 小练习与答案

**练习 1**：`EEPTPU_SA` 里 `hwbase0~3` 这四个地址，分别对应 TPU 内存的哪四段？  
**答**：`par`（参数/权重）、`in`（输入）、`tmp`（临时缓冲）、`out`（输出）。依据是 `eep_tpu_start_work` 里 `bin_type==2` 分支的注释，见 [eeptpu_sa.cpp:266-269](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L266-L269)。

**练习 2**：为什么 `eeptpu_init` 里要写 `eepif.mem_base_addr = mem_base; eepif.tpu_reg_base = tpureg_addr;`？不写会怎样？  
**答**：因为真正执行硬件读写的是底层对象 `eepif`，`EEPTPU_SA` 只是调度者。不把基址同步给 `eepif`，后续 `eepif.register_write(...)` 就不知道寄存器块映射在哪个物理地址，写入会落到错误地址导致总线错误或无效写入。

---

### 4.2 TPU 寄存器定义

#### 4.2.1 概念说明

TPU 的控制寄存器块被映射到 ARM 物理地址 `0xA0000000`（由 `EEPTPU_REG_BASE_ADDR` 指定，见 u1-l4 的 `assign_bd_address`）。这一块寄存器里有若干个 32 位寄存器，各自有一个**偏移量**（offset）。`config.h` 把「基地址 + 偏移」预先算好，定义成一组**指向 `volatile unsigned int` 的宏**，让代码可以直接像变量一样读写——这是裸机/嵌入式里访问内存映射寄存器的经典写法。

`volatile` 关键字至关重要：它告诉编译器「这个地址的内容随时会变（被硬件改），不要把对它的读取优化掉、不要缓存到寄存器」。轮询状态位时如果少了 `volatile`，编译器可能把 `while(rd_val & 0x80000000)` 优化成只读一次，程序就死在循环里了。

本模块涉及的寄存器分两类：

- **配置类**（forward 前写）：`BASEADDR0~3`、`ALGOADDR`。
- **控制/状态类**（forward 时用）：`STARTUP`（启动）、`STATUS`（完成标志）、`RUNTIMER`（硬件计时）。

#### 4.2.2 核心流程

寄存器偏移一览（本讲涉及的部分）：

| 宏名 | 偏移 | 类别 | 作用 |
|------|------|------|------|
| `EEPTPU_STATUS_REG` | `0x0C` | 状态 | bit31 = 推理完成标志 |
| `EEPTPU_RUNTIMER_REG` | `0x24` | 状态 | 硬件运行周期计数（调试用） |
| `EEPTPU_ALGOADDR_REG` | `0x30` | 配置 | 算法调度表起始地址 |
| `EEPTPU_STARTUP_REG` | `0x34` | 控制 | 写 `0x11` 触发一次推理启动 |
| `EEPTPU_BASEADDR0_REG` | `0x50` | 配置 | par（参数/权重）区基址 |
| `EEPTPU_BASEADDR1_REG` | `0x54` | 配置 | in（输入）区基址 |
| `EEPTPU_BASEADDR2_REG` | `0x58` | 配置 | tmp（临时）区基址 |
| `EEPTPU_BASEADDR3_REG` | `0x5C` | 配置 | out（输出）区基址 |

> 备注：硬件版本寄存器在偏移 `0x44`（见 [eeptpu_sa.cpp:190](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L190) 的 `eepreg_read(0x44, &rval)`），但 `config.h` 没有为它定义宏，所以代码里直接写裸偏移 `0x44`。

地址计算可以用一个式子概括，对任意寄存器宏 \(R\)：

\[
\text{物理地址}(R) = \text{EEPTPU\_REG\_BASE\_ADDR} + \text{offset}(R) = \text{0xA0000000} + \text{offset}(R)
\]

例如 `BASEADDR0` 的物理地址 \(= \text{0xA0000000} + \text{0x50} = \text{0xA0000050}\)。

#### 4.2.3 源码精读

**基地址与寄存器宏**全部集中在 `config.h` 顶部：

[config.h:24-35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L24-L35) —— 这十二行是本讲的「契约表」：
- 第 25 行 `EEPTPU_MEM_BASE_ADDR 0x31000000`：张量数据区基址（权重/输入/输出都落在它附近）。
- 第 26 行 `EEPTPU_REG_BASE_ADDR 0xA0000000`：寄存器块基址。
- 第 28 行 `EEPTPU_RUNTIMER_REG`：`*(volatile unsigned int *)(0xA0000000+0x24)`，硬件计时器。
- 第 29-32 行 `EEPTPU_BASEADDR0~3_REG`：偏移 `0x50/0x54/0x58/0x5C`，四段内存基址。
- 第 33 行 `EEPTPU_ALGOADDR_REG`：偏移 `0x30`，算法调度表地址。
- 第 34 行 `EEPTPU_STARTUP_REG`：偏移 `0x34`，启动寄存器。
- 第 35 行 `EEPTPU_STATUS_REG`：偏移 `0x0C`，状态寄存器。

读懂这组宏的关键，是明白它们都是**「把一个整数地址强转成 `volatile unsigned int*` 再解引用」的左值**，所以既能出现在 `=` 左边（写寄存器）也能出现在右边（读寄存器）。例如 `EEPTPU_STARTUP_REG = 0x11;` 就是「往 `0xA0000034` 写 `0x11`」。

**两种访问写法的对照**：`config.h` 的宏（如 `EEPTPU_BASEADDR0_REG`）和 `eeptpu_sa.cpp` 里的裸偏移（如 `eepreg_write(0x50, ...)`）指向的是**同一个物理寄存器**。前者直接解引用指针，后者经 `eepreg_write → eepif.register_write` 间接解引用；殊途同归。这也是为什么本讲会出现「两套 forward」——它们只是用了两种不同的寄存器访问风格。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：把「宏名 → 偏移 → 物理地址」三者对应起来。

**操作步骤**：

1. 在 `config.h:29-32` 找到 `EEPTPU_BASEADDR0~3_REG` 的偏移，手算它们的物理地址（基址 `0xA0000000`）。
2. 在 `eeptpu_sa.cpp:261-269` 找到 `eepreg_write(0x50, ...)` 等调用，确认这些裸偏移与上面四个宏一一对应。
3. 思考：如果把 `config.h:35` 的 `volatile` 去掉，`tpu_forward()` 里的轮询循环会出什么问题？

**预期结果 / 需要观察的现象**：
- `BASEADDR0~3` 的物理地址分别是 `0xA0000050 / 0xA0000054 / 0xA0000058 / 0xA000005C`。
- 去掉 `volatile` 后，编译器可能把 `rd_val = EEPTPU_STATUS_REG;` 的读取提到循环外只执行一次，导致 `while` 永远看不到硬件把 bit31 置 1，程序死循环。这是嵌入式开发经典坑。本项为「源码阅读型实践」，结论可在阅读层面确认，无需上板。

#### 4.2.5 小练习与答案

**练习 1**：`EEPTPU_ALGOADDR_REG` 的物理地址是多少？它存的是什么？  
**答**：\( \text{0xA0000000} + \text{0x30} = \text{0xA0000030} \)。它存的是「算法调度表」的起始地址（`addr_alg`），即 TPU bin 里描述「先算哪层、后算哪层」那张调度表在 DDR 中的位置。

**练习 2**：`STATUS`、`STARTUP`、`RUNTIMER` 三个寄存器，哪个是「写」、哪些是「读」？  
**答**：`STARTUP` 是写（ARM 写 `0x11` 启动推理）；`STATUS` 与 `RUNTIMER` 是读（ARM 读它们判断完成 / 获取耗时）。不过 `STATUS` 是否需要先写清零由 IP 内部约定，源码中 forward 流程只对它做读轮询。

---

### 4.3 forward 寄存器时序

#### 4.3.1 概念说明

有了类（4.1）和寄存器表（4.2），现在把它们串成一次真实的推理。所谓 forward 时序，就是**按照固定顺序往配置寄存器里写值，最后往 STARTUP 写一个启动字，TPU 就开始执行调度表描述的全部计算**。这就像给一台数控机床「设好工件坐标 → 设好加工程序入口 → 按下启动键」。

本工程有**两套等价的 forward 实现**，务必认清：

1. **类方法版**：`EEPTPU_SA::forward()` → 内部调用 `eep_tpu_start_work(addr_alg)` + `eep_tpu_wait_done()`，用 `eepreg_write` 风格写寄存器。
2. **main.cc 内联版**：`main.cc` 里的 `static void tpu_forward()`，直接用 `config.h` 的宏写寄存器。

`main.cc` 的菜单（case `'2'`、case `'5'`）实际调用的是**第 2 种** `tpu_forward()`，类方法 `forward()` 在本 main 里并未被调用（它是作为库式 API 保留的）。两者实现的是同一个协议，对照阅读能加深理解。

#### 4.3.2 核心流程

一次 forward 在寄存器层面发生的步骤（以当前 yolov4-tiny 工程的 `pub=2`、四基址为准）：

```text
[1] 写 BASEADDR0 (0x50) ← hwbase0      # par：参数/权重区
[2] 写 BASEADDR1 (0x54) ← hwbase1      # in：输入区
[3] 写 BASEADDR2 (0x58) ← hwbase2      # tmp：临时区
[4] 写 BASEADDR3 (0x5C) ← hwbase3      # out：输出区
[5] 写 ALGOADDR  (0x30) ← addr_alg     # 算法调度表入口
[6] 写 STARTUP   (0x34) ← 0x11         # 启动！TPU 开始执行调度表
-------- 此时 ARM 在 STATUS 上忙等 --------
[7] 读 STATUS   (0x0C)，直到 (val & 0x80000000) == 0x80000000   # bit31=1 表示完成
```

完成判定的逻辑可以用一个布尔条件表达。设状态读回值为 \(s\)，掩码 \(m = \text{0x80000000}\)，则完成条件为：

\[
(s \wedge m) = m \quad\Longleftrightarrow\quad s_{31} = 1
\]

也就是说，只要第 31 位被硬件置 1，就认为本次推理结束。注意：`enc=1`（两基址）类型只会执行步骤 [1][2][5][6][7]，跳过 [3][4]，这正是 `eep_tpu_start_work` 里 `if (bin_type==1) {...} else if (bin_type==2) {...}` 分支的区别。

#### 4.3.3 源码精读

**类方法版的启动函数**：

[eeptpu_sa.cpp:255-283](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L255-L283) —— `eep_tpu_start_work(unsigned long addr)`：
- `bin_type==1` 分支（259-263 行）：只写 `0x50`、`0x54` 两个基址。
- `bin_type==2` 分支（264-270 行）：写满 `0x50/0x54/0x58/0x5C` 四个基址，注释明确标注 par/in/tmp/out。
- 第 271 行 `eepreg_write(0x00000030, addr)`：写算法地址（`addr` 形参，调用时传的是 `addr_alg`）。
- 第 272 行 `eepreg_write(0x00000034, 0x00000011)`：**写启动字 `0x11`**，这是真正「按下启动键」的一行。`0x11` 是 IP 约定的启动命令值，其位含义由加密 IP 内部定义，源码未公开注释。

**类方法版的 forward 总装**：

[eeptpu_sa.cpp:295-306](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L295-L306) —— `forward()` 就是 `start_work(addr_alg)` 接 `wait_done()` 两步，是最精简的「启动 + 等完成」封装。

**main.cc 内联版**（实际被调用）：

[main.cc:190-228](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L190-L228) —— `tpu_forward()`：
- 第 195 行 `EEPTPU_BASEADDR0_REG = waddr;`（对应 `hwbase0`）。
- 第 200 行 `EEPTPU_BASEADDR1_REG = sd_input_addr;`（对应 `hwbase1`）。
- 第 205 行 `EEPTPU_BASEADDR2_REG = tpu_hwbase2;`。
- 第 210 行 `EEPTPU_BASEADDR3_REG = tpu_hwbase3;`。
- 第 215 行 `EEPTPU_ALGOADDR_REG = tpu_algbase;`（算法地址）。
- 第 220 行 `EEPTPU_STARTUP_REG = 0x11;`（启动字，与类方法版的 `0x11` 完全一致）。
- 第 222-227 行：读 `EEPTPU_STATUS_REG`，`while((rd_val & 0x80000000) != 0x80000000)` 忙等。

对照可见：`main.cc::tpu_forward()` **固定写满四个 BASEADDR**（即按 `pub=2` 行为写），而 `EEPTPU_SA::eep_tpu_start_work` 则**按 `bin_type` 分支**写 2 个或 4 个。两者写寄存器的值与顺序一致，只是 main.cc 版把分支固化了。

**启动前的地址准备**：

[main.cc:297-304](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L297-L304) —— `main` 在 `eeptpu_init` 之后，把 `eepsa.hwbase0~3`、`eepsa.addr_alg` 拷给 `waddr/sd_input_addr/tpu_hwbase2/tpu_hwbase3/tpu_algbase` 五个全局变量，供 `tpu_forward()` 使用。

**调用点**：

[main.cc:379-390](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L379-L390) —— 菜单 case `'2'`：用 `XTime_GetTime` 在 `tpu_forward()` 前后取样，算出软件侧耗时 `tused_forward`（微秒）。注意此处 `XTime` 是 ARM 的全局计时器，测量的是「启动 + 忙等 + 返回」整段时间，含 ARM 轮询开销，并非纯 TPU 计算时间（纯 TPU 计时要用 `RUNTIMER`，见 4.4）。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：对照 `tpu_forward()`（main.cc:190-228）与 `config.h` 的寄存器宏，写出一次推理在寄存器层面发生的完整步骤。

**操作步骤**：

1. 打开 [main.cc:190-228](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L190-L228)，逐行把每个 `EEPTPU_*_REG = xxx` 翻译成「写偏移 ← 值」。
2. 打开 [config.h:29-35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L29-L35)，给每个宏填上偏移。
3. 把 `waddr/sd_input_addr/tpu_hwbase2/tpu_hwbase3/tpu_algbase` 这些变量回溯到 `main.cc:297-304`，确认它们的来源是 `eepsa.hwbase0~3 / eepsa.addr_alg`。
4. 写出完整步骤清单（见下方「预期结果」）。

**预期结果**（这就是本讲要求你产出的「寄存器级推理步骤」）：

```text
步骤1: 写 偏移0x50 (BASEADDR0) ← waddr        = eepsa.hwbase0   (par 参数区)
步骤2: 写 偏移0x54 (BASEADDR1) ← sd_input_addr = eepsa.hwbase1   (in  输入区)
步骤3: 写 偏移0x58 (BASEADDR2) ← tpu_hwbase2   = eepsa.hwbase2   (tmp 临时区)
步骤4: 写 偏移0x5C (BASEADDR3) ← tpu_hwbase3   = eepsa.hwbase3   (out 输出区)
步骤5: 写 偏移0x30 (ALGOADDR)  ← tpu_algbase   = eepsa.addr_alg  (算法调度表入口)
步骤6: 写 偏移0x34 (STARTUP)   ← 0x11          ← 启动！TPU 开始执行
步骤7: 读 偏移0x0C (STATUS)，轮询直到 (val & 0x80000000) == 0x80000000  ← bit31=1 完成
```

> 说明：以上为「源码阅读型实践」，结论由阅读源码直接得出，无需运行硬件即可确认。若要在真实板卡上验证，需先按 u1-l3/u4-l1 完成上板与 `eeptpu_init`、权重加载，属于「待本地验证」的进阶环节。

#### 4.3.5 小练习与答案

**练习 1**：`EEPTPU_SA::eep_tpu_start_work` 和 `main.cc::tpu_forward` 都写了启动字，值是多少？为什么必须一致？  
**答**：都是 `0x11`（见 [eeptpu_sa.cpp:272](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L272) 与 [main.cc:220](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L220)）。必须一致是因为它们驱动的是同一个 TPU IP，`0x11` 是该 IP 约定的「启动一次推理」命令字，写别的值 IP 不会按预期启动。

**练习 2**：如果换成一个 `enc=1`（两基址）类型的网络，`main.cc::tpu_forward()` 还能正常工作吗？  
**答**：仍能工作但「写了多余的寄存器」。`tpu_forward()` 固定写满四个 BASEADDR，对 `enc=1` 网络而言 `BASEADDR2/3` 会被忽略（IP 不读）。而类方法版 `eep_tpu_start_work` 在 `bin_type==1` 时只写 `0x50/0x54`，更「干净」。所以换网络类型时，用类方法版更稳妥。

---

### 4.4 完成状态轮询

#### 4.4.1 概念说明

写完启动字之后，TPU 就开始按照调度表猛算。问题是：ARM 怎么知道它算完了？本项目用的是最简单粗暴的办法——**忙等（busy wait）/ 自旋轮询**：反复读 `STATUS` 寄存器（偏移 `0x0C`），直到它的第 31 位变成 1。

这种做法的好处是**实现极简、延迟最低**（一完成就立刻被发现，没有中断入栈出栈的开销）；代价是**ARM 在等待期间被完全占用**（CPU 占用率 100%），干不了别的事。对本工程来说，推理本来就是主线程唯一要做的事，忙等是合理选择。注意轮询循环里**没有超时机制**——如果硬件卡死，软件会死在 `while` 里出不来，这是裸机代码常见的取舍。

#### 4.4.2 核心流程

轮询的判定条件已在 4.3.2 给出：\( (s \wedge \text{0x80000000}) = \text{0x80000000} \)。

工程里同样有两套等价实现：

- 类方法版：`eep_tpu_wait_done()` 调 `eepreg_wait(0x0c, 0x80000000, 0x80000000)`，最终落到 `eepif.register_wait`。
- main.cc 版：直接 `while((rd_val & 0x80000000) != 0x80000000) { rd_val = EEPTPU_STATUS_REG; }`。

`register_wait(addr, mask, want_val)` 的语义是：反复读 `addr`，直到 `(读回值 & mask) == want_val` 才返回。这里 `mask = 0x80000000`、`want_val = 0x80000000`，意思是「只看 bit31，且要求它为 1」。

> 关于计时：本工程提供两种推理耗时度量。一是软件侧的 `XTime`（ARM 全局计时器），量到的是「启动→忙等→返回」总时长，含轮询开销；二是硬件侧的 `EEPTPU_RUNTIMER_REG`（偏移 `0x24`），由 TPU 自己统计纯计算周期。两者哪个更接近「纯 TPU 计算耗时」？是后者。这一对比会在 U5 的 u5-l1 详细展开，本讲只点到 `RUNTIMER` 的存在。

#### 4.4.3 源码精读

**类方法版的两层封装**：

[eeptpu_sa.cpp:285-292](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L285-L292) —— `eep_tpu_wait_done()`：一行 `eepreg_wait(0x0000000c, 0x80000000, 0x80000000)`，偏移 `0x0C` 即 `STATUS`，掩码与期望值都是 `0x80000000`（bit31）。

[eeptpu_sa.cpp:250-253](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L250-L253) —— `eepreg_wait` 把偏移或上 `tpureg_addr` 后交给 `eepif.register_wait`。

**底层轮询实现**（点至为止，详讲见 u4-l3）：

[eep_interface.cpp:74-95](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L74-L95) —— `register_wait`：`while(1) { register_read(addr, &rval); if ((rval & mask) == want_val) { ret=0; break; } }`。注意第 89 行的 `usleep(1000)` 被注释掉了——也就是说当前实现是**纯死循环、不带延时、不带超时**，一旦硬件不响应就会永久挂起。这是阅读本段最需要注意的一点。

**main.cc 内联版**：

[main.cc:222-227](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L222-L227) —— 先读一次 `EEPTPU_STATUS_REG` 进 `rd_val`，然后 `while((rd_val & 0x80000000) != 0x80000000) { rd_val = EEPTPU_STATUS_REG; ret = 0; }`，与底层 `register_wait` 逻辑一致，只是内联展开。

**硬件计时器的读取**（调试用）：

[main.cc:391-394](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L391-L394) —— 在 `EEP_DEBUG_INFO` 宏打开时，forward 之后读 `EEPTPU_RUNTIMER_REG` 打印硬件运行周期数。默认编译时这段被条件编译剔除，所以正常运行看不到这行打印。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：理解轮询的「掩码 + 期望值」语义，并发现潜在风险点。

**操作步骤**：

1. 读 [eep_interface.cpp:74-95](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L74-L95)，确认 `register_wait` 的退出条件是 `(rval & mask) == want_val`。
2. 追问自己：如果 `mask` 改成 `0x0`、`want_val` 改成 `0x0`，会发生什么？（提示：`(任何值 & 0) == 0` 恒成立，第一次循环就退出，等于「不等」。）
3. 找到第 89 行被注释的 `usleep`，思考：在一个带操作系统的版本里，把这段改成「轮询 + usleep」有什么好处？

**预期结果 / 需要观察的现象**：
- 当前实现是无延时、无超时的纯自旋，硬件故障会死锁。
- 加 `usleep` 可以让 CPU 在轮询间隙「喘口气」，降低占用率，适合多任务/带 OS 的场景；代价是完成响应延迟增加到最多一个 sleep 周期。本项为阅读型实践，结论可在源码层面直接确认。

#### 4.4.5 小练习与答案

**练习 1**：`eepreg_wait(0x0c, 0x80000000, 0x80000000)` 里，掩码和期望值为什么都填 `0x80000000`？能否把期望值填 `0x0`？  
**答**：都填 `0x80000000` 表示「只考察 bit31，并要求它为 1」才算完成。如果把期望值填 `0x0`，语义就变成「要求 bit31 为 0」才退出——这与硬件「完成后置 1」的约定相反，会在推理还没开始（bit31=0）的瞬间立刻误判为「完成」。所以不能乱填。

**练习 2**：软件 `XTime` 计出的 `tused_forward` 和硬件 `EEPTPU_RUNTIMER_REG` 哪个更大？为什么？  
**答**：通常 `XTime` 计出的更大。因为 `XTime` 包含了「写寄存器启动 + ARM 忙等轮询 + 函数返回」的全部时间，而 `RUNTIMER` 是 TPU 内部统计的纯计算周期。`XTime` 还会受到 CPU 频率、轮询开销、缓存状态等影响，`RUNTIMER` 更贴近 TPU 真实算力。（精确对比留待 u5-l1。）

---

## 5. 综合实践

把四个模块串起来，完成下面这个贯穿本讲的小任务。

**任务**：假设你是新接手这块裸机代码的工程师，被要求在代码评审会上讲清楚「从用户在串口菜单按下 `2`，到屏幕打印出 forward 耗时，这中间 TPU 寄存器到底经历了什么」。请产出一份**寄存器级时序说明**，并结合源码行号给出依据。

**建议产出格式**（你可以填完它）：

```text
【前置】main.cc 启动时已完成：
  - eeptpu_init(...) 解析 eepnet_config，填好 hwbase0~3、addr_alg（eeptpu_sa.cpp:102-215）
  - 把它们拷给 waddr/sd_input_addr/tpu_hwbase2/tpu_hwbase3/tpu_algbase（main.cc:297-304）
  - 权重已从 SD 卡读到 waddr（main.cc:314），输入已写到 sd_input_addr

【按下 2 后】case '2' 分支（main.cc:379-398）调用 tpu_forward()（main.cc:190-228）：
  写 BASEADDR0(0x50) ← waddr         # par
  写 BASEADDR1(0x54) ← sd_input_addr # in
  写 BASEADDR2(0x58) ← tpu_hwbase2   # tmp
  写 BASEADDR3(0x5C) ← tpu_hwbase3   # out
  写 ALGOADDR (0x30) ← tpu_algbase   # 算法调度表
  写 STARTUP  (0x34) ← 0x11          # 启动
  轮询 STATUS (0x0C) 直到 bit31=1    # 完成

【返回后】XTime 计时得到 tused_forward（main.cc:382-385），随后 read_forward_result 取输出。
```

**进阶思考题**（选做）：

1. 如果你想把 `tpu_forward()` 改成「带超时」的版本（比如轮询超过 100ms 就报错返回 `eeperr_Timeout`），应该改哪个函数？给出改动位置（提示：`eep_interface.cpp:74-95` 的 `register_wait`，给它加一个循环计数上限）。
2. `EEPTPU_SA::forward()`（类方法版）在本 `main.cc` 里并没有被调用，却保留在类里。请讨论：保留它的价值是什么？（提示：作为可复用的库式 API，供其它裸机工程或更整洁的调用方使用；也让协议逻辑有一个不依赖全局变量的、可分支处理 `bin_type` 的「干净版本」。）

> 本综合实践为「源码阅读 + 设计讨论型」，无需运行硬件。若要在板卡上实测寄存器值，可在 `config.h` 打开 `EEP_DEBUG_INFO`，利用 `tpu_forward()` 里已有的 `#ifdef EEP_DEBUG_INFO` 打印块（main.cc:196-219）观察每个寄存器写入了什么——这是项目自带的调试手段。

## 6. 本讲小结

- `EEPTPU_SA` 是裸机版 TPU 对象，持有 `hwbase0~3`、`addr_alg`、`addr_out`、`addr_in`、`mean/norm`、`bin_type` 等全部推理上下文，并把真正的寄存器/内存读写委托给成员 `eepif`（`EEP_INTERFACE`）。
- TPU 控制寄存器块基址 `0xA0000000`（`EEPTPU_REG_BASE_ADDR`），关键偏移：`BASEADDR0~3 = 0x50/0x54/0x58/0x5C`、`ALGOADDR = 0x30`、`STARTUP = 0x34`、`STATUS = 0x0C`、`RUNTIMER = 0x24`，全部以 `volatile` 宏形式定义在 `config.h`。
- forward 协议是「写 4 个 BASEADDR → 写 ALGOADDR → 写 STARTUP=0x11 → 轮询 STATUS 的 bit31 直到为 1」；`enc=1` 类型只写 2 个 BASEADDR。
- 工程有两套等价实现：类方法 `EEPTPU_SA::forward()`（按 `bin_type` 分支，更通用）与 `main.cc::tpu_forward()`（固定写 4 个 BASEADDR，用宏直写寄存器，是菜单实际调用的版本）。
- 完成判定靠忙等轮询，`eepreg_wait(0x0c, 0x80000000, 0x80000000)` 即「STATUS 的 bit31 必须为 1」；底层 `register_wait` 无延时、无超时，硬件卡死会死锁。
- 软件耗时用 `XTime`（含轮询开销），纯 TPU 计算耗时看 `EEPTPU_RUNTIMER_REG`，两者差异在 u5-l1 详谈。

## 7. 下一步学习建议

- **紧接着读 u4-l3（eep_interface：AXI 内存与寄存器读写）**：本讲把 `eepif.register_write/register_wait` 当黑盒用了，下一讲会打开 `EEP_INTERFACE` 的实现，讲清 `mem_read/write`、`register_read/write/wait` 和 `round_up` 对齐宏，补全「寄存器写到底是怎么落到 AXI 总线上」这一层。
- **然后进入 U5**：u5-l1 会深入对比软件 `XTime` 与硬件 `RUNTIMER` 两种计时；u5-l2 会讲 `read_forward_result` 如何把输出地址上的 epmat（16 通道分组、32 字节步长、定点 `exp`）还原成 `ncnn::Mat`——也就是本讲 `BASEADDR3` 指向的 out 区到底怎么被读回来。
- **想动手验证**：在 `config.h` 里 `#define EEP_DEBUG_INFO`，重新编译上板，就能看到 `tpu_forward()` 内置的寄存器写值打印（main.cc:196-219），把本讲的寄存器时序在真实硬件上对照一遍。
- **横向对照 Linux 路线**：回头看 u2-l3 的 `EEPTPU::forward()`，会发现高层 API 把本讲的「写一堆寄存器 + 轮询」全藏进了一行调用——这正是「裸机驱动」与「运行库封装」两种路线的本质差异。
