# eep_interface：AXI 内存与寄存器读写

## 1. 本讲目标

上一讲（u4-l2）我们把裸机侧驱动 TPU 的「寄存器协议」讲透了：写 4 个 BASEADDR → 写 ALGOADDR → 写 STARTUP=0x11 → 轮询 STATUS 的 bit31。但那讲里的「写寄存器」到底是怎么落到硬件上的？「读输出张量」又是怎么从 DDR 里把字节搬回来的？本讲就回答这个最底层的问题。

学完本讲，你应当能够：

- 说清 `EEP_INTERFACE` 类在整个裸机软件分层里的位置，以及它为什么值得单独成一层。
- 看懂 `mem_write / mem_read / register_write / register_read / register_wait` 这五个接口的实现，理解它们在裸机下为何就是「一行指针解引用」。
- 解释 `register_wait` 的 `mask` 与 `want_val` 两个参数的确切含义，并能自己写出一个新的等待条件。
- 读懂 `round_up` 这个来自 Linux 内核的对齐宏，理解它为何服务于 TPU 的「16 通道分组」。

本讲是 u4-l2 的向下钻取，也是 u5-l1（forward 时序）与 u5-l2（输出读取）的底层基石。

## 2. 前置知识

在进入源码前，先建立两个关键直觉。

**直觉一：裸机里物理地址就是 C 指针。**
在 Linux 里，用户态程序看到的地址是虚拟地址，直接 `*(char*)0x31000000` 一定会段错误——你必须经 `/dev/mem` 或驱动（如 XDMA）才能摸到硬件。但裸机程序没有操作系统、通常也没有开 MMU（或开了 1:1 平坦映射），所以「物理地址 == 可解引用的指针」。这意味着 `0xA0000000` 这个寄存器块基址、`0x31000000` 这个张量数据区基址，在 C 代码里可以直接当成指针用：

```c
*(volatile unsigned int*)0xA0000034 = 0x11;   // 直接往 STARTUP 寄存器写 0x11
```

这一行在裸机下是真实可用的，它就是 `EEP_INTERFACE` 一切操作的底层本质。

**直觉二：`volatile` 是内存映射 I/O 的命根子。**
编译器看到「先写一个地址、又在循环里反复读同一个地址」，会自作主张地把读优化掉（它认为没人改这个地址、读一次就够了）。但对硬件寄存器，每次读都可能拿到不同的值（比如 STATUS 寄存器的 done 位会从 0 变 1）。`volatile` 关键字就是告诉编译器：「这个地址的访问有副作用，每次都要老老实实生成一条 load/store 指令」。本讲的所有读写函数里，指针一律带 `volatile`，原因就在此。

承接 u4-l2：两条 AXI 通路——控制通路（PS→TPU 寄存器，落在 `0xA0000000`）与数据通路（TPU→DDR 张量，落在 `0x31000000`）——在本讲里分别对应 `register_*` 与 `mem_*` 两组接口。

## 3. 本讲源码地图

本讲只围绕「一个类 + 一个宏」展开，涉及文件极少但极关键：

| 文件 | 作用 |
| --- | --- |
| [sdk/standalone/src/eeptpu/interface/eep_interface.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h) | `EEP_INTERFACE` 类声明、`round_up` 宏、错误码定义 |
| [sdk/standalone/src/eeptpu/interface/eep_interface.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp) | 五个接口的实现，本讲的主战场 |
| [sdk/standalone/src/eeptpu/eeptpu_sa.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h) | `EEPTPU_SA` 持有 `EEP_INTERFACE eepif` 成员（第 70 行） |
| [sdk/standalone/src/eeptpu/eeptpu_sa.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp) | 通过 `eepif.*` 调用底层接口的上层代码 |
| [sdk/standalone/src/config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h) | `EEPTPU_MEM_BASE_ADDR`/`EEPTPU_REG_BASE_ADDR` 等魔法地址 |

记忆要点：`eep_interface` 是「怎么写」，`eeptpu_sa` 是「写什么」，`config.h` 是「写到哪」。

## 4. 核心概念与源码讲解

### 4.1 EEP_INTERFACE 在分层设计中的位置

#### 4.1.1 概念说明

裸机驱动 TPU 的代码天然分成两层：

- **上层（`EEPTPU_SA`）**：知道「TPU 的寄存器协议」——先写哪几个寄存器、写什么值能启动推理、轮询哪个寄存器的哪一位判断完成。这层关心的是 **what**。
- **下层（`EEP_INTERFACE`）**：只知道「往一个地址写一个 32 位整数」「从一段地址搬一堆字节」。它对 TPU 一无所知，是个纯粹的「地址—数据」搬运工。这层关心的是 **how**。

`EEPTPU_SA` 通过持有一个 `EEP_INTERFACE eepif` 成员来调用下层，而不是自己直接操作指针。这种分层的最大价值是**可移植**：今天裸机用 AXI 内存映射（指针直接解引用），明天要换成 PCIe/XDMA 卡（要走 `/dev/xdma0_*` 字符设备），你只需要重写 `EEP_INTERFACE` 的五个方法，上层 `EEPTPU_SA` 一行都不用动。

> 与 Linux 路线对照：Linux 的 `libeeptpu_pub` 运行库里，同样的 `EEP_INTERFACE` 抽象对应的是「SoC 模式」与「PCIE 模式」两套后端实现（见 u2-l4）。裸机仓库里我们只看到 AXI 这一套，但接口形状是统一的。

#### 4.1.2 核心流程

从上层菜单「2: forward」到底层硬件的完整调用链：

```
main.cc 菜单 '2'
  └─ tpu.forward()                          // EEPTPU_SA 类方法（eeptpu_sa.cpp:295）
       ├─ eep_tpu_start_work(addr_alg)      // 写 BASEADDR×4、ALGOADDR、STARTUP
       │    └─ eepreg_write(offset, val) ×N // 上层包装：offset → 绝对地址
       │         └─ eepif.register_write(tpureg_addr|offset, val)
       │              └─ *(volatile uint*)addr = val;   // ← 真正的硬件访问
       └─ eep_tpu_wait_done()
            └─ eepreg_wait(0xc, 0x80000000, 0x80000000)
                 └─ eepif.register_wait(addr, mask, want_val)
                      └─ while(1) register_read(addr) 直到 (rval & mask) == want_val

main.cc 读结果
  └─ tpu.read_forward_result(outputs)       // eeptpu_sa.cpp:364
       └─ eepif.mem_read(addr_out[i].hwaddr, epmat, size)
            └─ while(i<size) *pdst++ = *psrc++;   // ← 从 DDR 搬字节
```

注意链路上的「地址拼接」发生在 `eepreg_*` 这层包装里：上层只传「寄存器偏移」（如 `0x50`），包装层把它和基址 `tpureg_addr`（`0xA0000000`）拼成绝对地址，再交给 `eepif`。所以 `eepif` 拿到的永远是绝对地址，它不需要知道任何「基址 + 偏移」的约定。

#### 4.1.3 源码精读

`EEP_INTERFACE` 的类声明极其精简，只有五个公共方法和两个公共成员变量：

[eep_interface.h:26-43](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h#L26-L43)：声明 `mem_write/mem_read/register_write/register_read/register_wait` 五个接口，以及 `tpu_reg_base`、`mem_base_addr` 两个成员变量（裸机模式下这两个成员其实未被使用，是为 PCIE 等其他后端保留的）。

`EEPTPU_SA` 以成员形式持有一个接口实例：

[eeptpu_sa.h:69-70](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.h#L69-L70)：`EEP_INTERFACE eepif;` —— 这一行就是上下两层的耦合点，整个 `EEPTPU_SA` 对硬件的所有访问都经由此成员。

初始化时，上层把基址同步给下层（虽然在裸机 AXI 模式下 `eepif` 不依赖这两个值，但写上以保持接口一致）：

[eeptpu_sa.cpp:186-187](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L186-L187)：`eepif.mem_base_addr = mem_base;` 与 `eepif.tpu_reg_base = tpureg_addr;`，把 `0x31000000` 与 `0xA0000000` 填入接口对象。

#### 4.1.4 代码实践

**实践目标**：用源码阅读确认「上层只传偏移、下层拿到绝对地址」这条契约。

**操作步骤**：

1. 打开 [eeptpu_sa.cpp 的 eepreg_write](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L244-L247)，看清它如何把 `regaddr` 与 `tpureg_addr` 做按位或。
2. 打开 [eeptpu_sa.cpp 的 eep_tpu_start_work](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L255-L283)，数一数它连续调用了几次 `eepreg_write`，每次的偏移分别是多少（`0x50/0x54/0x58/0x5c/0x30/0x34`）。
3. 心算验证：`tpureg_addr=0xA0000000`、`regaddr=0x34` 时，`0xA0000000 | 0x34 == 0xA0000034`，与 `config.h` 里 `EEPTPU_STARTUP_REG` 展开后的地址完全一致。

**需要观察的现象**：上层的「逻辑寄存器名」（STARTUP）= 下层的「绝对地址」`0xA0000034`，二者通过「基址 | 偏移」严格对应。

**预期结果**：你会确认 `eepreg_write` 的 `|`（按位或）在这里与 `+`（加法）等价，因为 `0xA0000000` 的低 8 位全是 0、而偏移量小于 `0x100`，二者不重叠。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `EEPTPU_SA::eepreg_write` 里的 `tpureg_addr | regaddr` 改成 `tpureg_addr + regaddr`，行为会变吗？

**答案**：在本工程里不会变（结果完全相同），因为基址低 8 位为 0、偏移 < 0x100，按位或与加法结果一致。但这是一种**脆弱**的写法：一旦某天基址不是按位对齐的（比如基址低几位非 0），`|` 与 `+` 就会分叉。注释里写「use register offset」正是强调传进来的是偏移而非绝对地址。

**练习 2**：为什么 `EEP_INTERFACE` 的两个成员变量 `tpu_reg_base`、`mem_base_addr` 在裸机 AXI 模式下基本没起作用？

**答案**：因为裸机模式下所有地址都由上层 `EEPTPU_SA` 在调用前拼成绝对地址直接传进来了（`mem_write(addr,...)` 的 `addr` 已经是 `0x31000000` 一类的绝对值），`eepif` 实现里直接解引用 `addr`，不需要再去查 `mem_base_addr`。这两个成员是为「后端自己管地址」（如 PCIE 模式下基址填 0、寻址交给 xdma 引擎）保留的，参见 u2-l4。

---

### 4.2 内存读写语义：mem_write / mem_read

#### 4.2.1 概念说明

`mem_write` 与 `mem_read` 服务于**数据通路**——把张量字节流在 ARM 内存与 TPU 可见的 DDR 之间搬运。典型用途有两个：

- **加载权重**：把 `eepnet.mem`（约 12 MB 权重）从 ARM 内存写到 `hwbase0/hwbase1` 指向的 DDR 区。
- **读取输出**：推理完成后，把 TPU 写在 `addr_out[i].hwaddr` 的输出张量字节读回 ARM 内存，交给 `epmat2nmat` 反量化。

注意：在裸机 1:1 映射下，「写到 `hwbase0`」和「TPU 从同一个 DDR 物理地址读」是同一块物理内存的两个视角——ARM 写进去的字节，TPU 立刻能看到（缓存一致性需手动维护，见 u4-l1 提到的 `Xil_DCacheFlush`）。

#### 4.2.2 核心流程

两个函数都是**逐字节循环拷贝**，用 `volatile unsigned char*` 指针：

```c
// mem_write 的本质：把 datbuf 的 buf_size 字节写到 addr
volatile unsigned char* pdst = (volatile unsigned char*)addr;
for (i = 0; i < buf_size; i++) *pdst++ = *psrc++;
```

逐字节（而非 `memcpy` 或 4 字节字）拷贝的好处是**不要求地址对齐**——TPU 的张量地址可能不是 4 字节对齐的，逐字节最稳妥。代价是慢，但权重只在启动时加载一次、输出张量也不大，可接受。

#### 4.2.3 源码精读

[eep_interface.cpp:34-45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L34-L45)：`mem_write` 实现。注意它的参数表里有 `loadlen` 和 `buf_size` 两个长度，但循环只用 `buf_size`——`loadlen` 是历史遗留的未用参数，调用方传入时通常两者相等。

[eep_interface.cpp:47-58](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L47-L58)：`mem_read` 实现，方向相反，从 `addr` 读 `readlen` 字节到 `readbuf`。

上层加载权重的调用点：

[eeptpu_sa.cpp:207-212](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L207-L212)：当未定义 `SD_CARD_IS_READY` 时，`eeptpu_init` 用 `eepif.mem_write(wraddr, datalen, data, datalen)` 把编译期烧入的权重数组 `data` 写到 DDR。

上层读输出的调用点：

[eeptpu_sa.cpp:373-382](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L373-L382)：`read_forward_result` 先 `malloc` 一块 `epmat_size` 的缓冲，再 `eepif.mem_read(addr_out[i].hwaddr, epmat, epmat_size)` 把硬件输出读进来，最后交给 `epmat2nmat`。

#### 4.2.4 代码实践

**实践目标**：理解 `mem_write` 参数表里两个长度的真实作用，并确认权重加载的内存方向。

**操作步骤**：

1. 在 [eep_interface.cpp:34-45](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L34-L45) 里数清楚循环边界是 `buf_size` 而非 `loadlen`。
2. 跳到调用点 [eeptpu_sa.cpp:209](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L209)，看调用方传的是 `eepif.mem_write(wraddr, datalen, data, datalen)`——`loadlen` 与 `buf_size` 都填了 `datalen`，所以「未用参数」在此无害。

**需要观察的现象**：即便你故意把第一个长度参数（`loadlen`）改成 0 或任意值，函数行为不变，因为循环只看 `buf_size`。

**预期结果**：确认这是一个「接口签名保留了冗余参数、但实现只用其中一个」的实例。这是阅读真实工程代码时常碰到的「历史包袱」。

**待本地验证**：若你想在板上实测 `mem_read` 的正确性，可参考 [eeptpu_sa.cpp:78-95](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L78-L95) 那段被 `#if 0` 关掉的回读校验代码——把 `#if 0` 改成 `#if 1` 即可在权重加载后回读若干字节做 hex dump 比对（需要硬件，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mem_write/mem_read` 用逐字节 `unsigned char*` 拷贝，而不是 `memcpy` 或 4 字节 `unsigned int*`？

**答案**：两个原因。其一，TPU 张量地址不一定 4 字节对齐，逐字节最安全（`unsigned int*` 解引用未对齐地址在某些架构上会触发异常）。其二，`volatile` 语义下标准库 `memcpy` 不保证逐字节访问、可能被优化成向量指令，对内存映射区不可控；手写 `volatile` 循环能确保每次访问都是真实的单字节 store/load。

**练习 2**：`mem_write` 把权重写到 `hwbase0` 后，TPU 一定能立刻读到吗？

**答案**：不一定。如果 ARM 的 D-cache 开启，写入可能停留在缓存而未落 DDR，TPU（经 HP 口直接访问 DDR）就会读到旧数据。所以裸机侧在关键写后必须 `Xil_DCacheFlush`，或在初始化时干脆 `Xil_DCacheDisable`（u4-l1 的启动序列里有关 D-cache 的处理）。这正是裸机无操作系统代管缓存带来的额外责任。

---

### 4.3 寄存器读写与等待：register_write / register_read / register_wait

#### 4.3.1 概念说明

这三个函数服务于**控制通路**——读写 TPU 的 32 位控制/状态寄存器（`0xA0000000` 块）。`register_write/read` 是单字（4 字节）访问；`register_wait` 是「轮询直到某一位/某几位变成期望值」的阻塞等待，是裸机没有 OS 调度时最朴素的状态同步手段。

#### 4.3.2 核心流程

`register_write` 与 `register_read` 都是单条指针解引用：

```c
int register_write(unsigned long addr, unsigned int wr_val) {
    *(volatile unsigned int*)addr = wr_val;   // 一条 store 指令
    return 0;
}
int register_read(unsigned long addr, unsigned int* rd_val) {
    *rd_val = *(volatile unsigned int*)addr;  // 一条 load 指令
    return 0;
}
```

`register_wait` 是一个**无超时、无 sleep 的忙等死循环**：

```c
while (1) {
    register_read(addr, &rval);
    if ((rval & mask) == want_val) { ret = 0; break; }
}
```

`mask` 与 `want_val` 的语义是：把读回值 `rval` 与 `mask` 按位与，只保留关心那些位，再判断是否等于 `want_val`。这是一个通用的「位字段等待」原语，既可以等单 bit，也可以等多 bit 字段。

#### 4.3.3 源码精读

[eep_interface.cpp:60-65](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L60-L65)：`register_write`，注意第 62 行有一条 `printf` 打印每次写的地址与值——这是排错时的利器，但也意味着频繁写寄存器会刷屏。

[eep_interface.cpp:67-72](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L67-L72)：`register_read`，其 `printf` 被注释掉了——这非常重要，因为 `register_wait` 内部会高频反复调用 `register_read`，若它的 printf 不注释，串口会被瞬间灌爆。

[eep_interface.cpp:74-95](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L74-L95)：`register_wait`。第 84 行 `(rval & mask) == want_val` 是核心判定；第 89 行被注释的 `usleep(1000)` 说明作者考虑过、但最终选择了最紧的忙等。

实际调用点（TPU 完成等待）：

[eeptpu_sa.cpp:285-292](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L285-L292)：`eep_tpu_wait_done()` 调用 `eepreg_wait(0x0000000c, 0x80000000, 0x80000000)`——即轮询 STATUS 寄存器（偏移 `0x0C`），等 bit31 变成 1。

#### 4.3.4 代码实践

**实践目标**：彻底搞懂 `mask` 与 `want_val` 的含义，并能自己设计新的等待条件。

**操作步骤**：

1. 打开 [eep_interface.cpp:81-92](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L81-L92)，把第 84 行的判定 `(rval & mask) == want_val` 在脑子里展开成两个真实场景（见下）。
2. 对照调用点 [eeptpu_sa.cpp:288](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L288)，确认 TPU 完成判据是「STATUS 的 bit31 == 1」。

**需要观察的现象与预期结果**：

- **场景 A（TPU 完成位）**：`mask=0x80000000`、`want_val=0x80000000`。读回 `rval=0x80000003` 时，`rval & 0x80000000 = 0x80000000 == want_val` → 满足、退出。读回 `rval=0x00000003` 时，`rval & 0x80000000 = 0 ≠ want_val` → 继续轮询。判据只看 bit31，对低位的其它状态位（如 `0x00000003`）视而不见。
- **场景 B（多 bit 字段，假设）**：如果想等一个 3 bit 的状态字段（bit[2:0]）变成 `0b101=5`，就传 `mask=0x00000007`、`want_val=0x00000005`。读回 `rval=...100000101`（低 3 位是 101）时满足。这演示了 `register_wait` 是通用原语，不局限于单 bit。

**待本地验证**：实际在板上运行时，串口会先看到 `register write: addr 0xa0000034, val 0x00000011`（启动），随后看到 `reg waiting: addr 0xa000000c, want 0x80000000`（开始轮询），最后菜单回到选项提示——这条日志序列就是一次完整 forward 的「软件侧证据」（需要硬件实测，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`eepreg_wait(0xc, 0x80000000, 0x80000000)` 里为什么 `mask` 和 `want_val` 都是 `0x80000000`？能不能把 `mask` 省略？

**答案**：`mask=0x80000000` 表示「只看 bit31」，`want_val=0x80000000` 表示「期望该位为 1」。两者数值相同只是巧合——因为这里关心的是单 bit 且期望值为 1。若期望 bit31 为 0，就会是 `mask=0x80000000, want_val=0x00000000`，此时两者不同，可见 `mask` 不能省略。`mask` 的作用是「屏蔽掉不关心的位」，让判定只聚焦在状态字段上。

**练习 2**：`register_wait` 没有 `timeout` 参数、循环里也没有 `sleep`，这会带来什么风险？

**答案**：两个风险。其一，若 TPU 因硬件故障永远不拉高 done 位，软件会**永久死循环**、整个裸机系统挂死，无法恢复（裸机没有看门狗踢狗以外的保护）。其二，紧忙等会**占满 CPU**、功耗高，且阻塞了同优先级的其它工作。注释掉的 `usleep(1000)` 是作者留下的「若想降低轮询频率可放开」的开关。生产级固件通常会加一个超时计数器，超时后返回 `eeperr_Timeout`（错误码已在头文件 [eep_interface.h:57](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h#L57) 预留为 `-8`）。

**练习 3**：为什么 `register_read` 里的 `printf` 被注释掉，而 `register_write` 里的 `printf` 保留？

**答案**：因为 `register_wait` 内部会在一个紧凑循环里反复调用 `register_read`——若它的 printf 开着，串口会以极高频率被灌满「register read...」，既拖慢轮询（printf 是慢 I/O）又淹没有用信息。`register_write` 只在协议启动阶段被调用有限几次，打印它有助于排错且不会刷屏。这是一个工程上很实用的「热路径静默、冷路径可观测」取舍。

---

### 4.4 地址对齐宏 round_up

#### 4.4.1 概念说明

`round_up(x, y)` 把 `x` 向上取整到 `y` 的整数倍（要求 `y` 是 2 的幂）。它不是 `EEP_INTERFACE` 的成员，而是定义在 [eep_interface.h:45-46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h#L45-L46) 的全局宏，供 `epmat_get_size` 等地方计算 TPU 张量的对齐尺寸。

为什么需要对齐？因为 TPU 硬件把通道按 **16 个一组**打包（每组每像素占 32 字节 = 16 通道 × 2 字节）。即便网络真实通道数是 255，硬件也会按 `round_up(255,16)=256` 来分配存储——少了那 1 个真实通道也要补齐 1 个填充通道，凑满 16 的整数倍。这是贯穿 u4-l4（输入预处理）与 u5-l2（输出读取）的核心约束，本讲只看它的数学实现。

#### 4.4.2 核心流程

`round_up` 借鉴自 Linux 内核的位运算对齐技巧。对 `y = 2^k`：

\[ \text{round\_up}(x,\, 2^k) \;=\; \big((x-1)\ \,|\ \,(2^k - 1)\big) + 1 \]

直觉解释：先 `x-1` 退一格，再用低 k 位全 1 的掩码 `2^k-1` 把低 k 位一次性「填满」，最后 `+1` 进位——结果就是「≥ x 的最小 2^k 整数倍」。例如 `y=16=2^4`，掩码为 `0x0F=15`：

| x | (x-1) | (x-1)\|0x0F | +1 = round_up(x,16) |
| --- | --- | --- | --- |
| 16 | 15 = 0x0F | 0x0F | 16 |
| 17 | 16 = 0x10 | 0x1F | 32 |
| 255 | 254 = 0xFE | 0xFF | 256 |

这个宏比「`(x + y - 1) / y * y`」的除法写法更高效——位运算没有除法指令，在嵌入式上更省 cycle。

#### 4.4.3 源码精读

[eep_interface.h:45-46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h#L45-L46)：宏定义本体。`__typeof__` 让掩码与 `x` 同类型，避免整型符号问题。

`round_up` 的实际使用点在 `epmat_get_size`：

[eeptpu_sa.cpp:309-313](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L309-L313)：`epmat_get_size` 用 `round_up(c, 16) * 2` 计算输出张量的字节数——通道数 `c` 先向上对齐到 16 的倍数，再乘以「每通道 2 字节（int16 定点）」。这与 `register_wait` 的 16 通道分组、`mem_read` 的字节搬运形成闭环：`round_up` 算出对齐尺寸 → `mem_read` 按此尺寸从 DDR 搬字节 → `epmat2nmat` 按 16 通道一组反量化（详讲见 u5-l2）。

#### 4.4.4 代码实践

**实践目标**：手算验证 `round_up`，并理解它在 TPU 张量尺寸计算里的角色。

**操作步骤**：

1. 打开 [eep_interface.h:45-46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h#L45-L46)，按上表的 3 行手算一遍。
2. 打开 [eeptpu_sa.cpp:309-313](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L309-L313)，代入 yolov4-tiny 的输出 shape `[1,255,13,13]`（即 `c=255, h=13, w=13`）：`round_up(255,16)=256`，`epmat_size = 13*13*256*2 = 86528` 字节。

**需要观察的现象**：真实通道是 255，但硬件占用 256 通道的存储——多出的 1 个通道是填充，对应 u4-l4 讲过的「16 通道对齐」。

**预期结果**：`read_forward_result` 会调用 `mem_read` 搬运这 86528 字节，其中有效数据只占 `255/256`，约 99.6%，剩余为填充。

#### 4.4.5 小练习与答案

**练习 1**：`round_up` 为什么要求 `y` 是 2 的幂？若传 `y=10` 会怎样？

**答案**：因为「`| (y-1)` 再 `+1`」的位运算技巧依赖 `y-1` 是「低 k 位全 1」的掩码，这只在 `y=2^k` 时成立。若 `y=10`，`y-1=9=0b1001` 不是连续低位全 1，结果错误。这也是宏里没有校验 `y` 是否为 2 的幂的原因——它把约束隐式甩给调用方（Linux 内核的同一宏也是如此）。

**练习 2**：`epmat_get_size` 里特判了 `if (h==1 && w==1) return round_up(c,16)*2;`，为什么这个分支没有乘 `h*w`？

**答案**：当 `h==1 && w==1`（全连接层输出或全局特征向量），每「像素」只有 1 个位置，张量字节数就是「对齐后的通道数 × 2 字节」，不乘 `h*w`（因为 `h*w=1`，乘了也一样，分支只是写得更显式）。一般分支 `h*w*round_up(c,16)*2` 才是「像素数 × 对齐通道 × 2」。该特判主要是为了和 `epmat2nmat_simple`（一维情况，[eeptpu_sa.cpp:314-329](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L314-L329)）的布局对齐。

---

## 5. 综合实践

本讲的核心实践任务是把上下两层的调用链亲手串一遍，并用 `register_wait` 的参数设计一个新的等待条件。

### 任务一：追踪 forward / read_forward_result 经 eepif 落到硬件的全过程

**实践目标**：验证「上层协议 → `eepif` 接口 → 指针解引用」这条链路，确认 `EEP_INTERFACE` 确实是唯一硬件出口。

**操作步骤**：

1. 从 [eeptpu_sa.cpp:295-306](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L295-L306) 的 `EEPTPU_SA::forward()` 出发，它先调 `eep_tpu_start_work(addr_alg)`、再调 `eep_tpu_wait_done()`。
2. 展开 `eep_tpu_start_work`（[eeptpu_sa.cpp:255-283](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L255-L283)）：它按 `bin_type` 分支，连续 `eepreg_write` 写 BASEADDR0~3（`0x50/0x54/0x58/0x5c`）、ALGOADDR（`0x30`）、STARTUP（`0x34`=0x11）。
3. 展开 `eepreg_write`（[eeptpu_sa.cpp:244-247](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L244-L247)）：它把 `tpureg_addr | regaddr` 拼成绝对地址，调 `eepif.register_write`。
4. 展开 `eepif.register_write`（[eep_interface.cpp:60-65](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L60-L65)）：最终落到 `*(volatile unsigned int*)addr = wr_val;`。
5. 对称地追踪 `read_forward_result`（[eeptpu_sa.cpp:364-387](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L364-L387)）：`epmat_get_size`（用 `round_up`）算尺寸 → `eepif.mem_read`（[eep_interface.cpp:47-58](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.cpp#L47-L58)）逐字节从 DDR 搬回 → `epmat2nmat` 反量化。

**需要观察的现象**：你会看到 `EEPTPU_SA` 全程没有出现一次「裸指针解引用」——所有硬件访问都收敛到 `eepif` 的五个方法。这正是分层的好处。

**预期结果**：画出一张调用树（如 4.1.2 的流程图），叶节点全是 `eepif.*`。如果某天要移植到 PCIE，只改叶节点实现即可。

### 任务二：两条等价路径的对照

**实践目标**：发现仓库里其实有「两条等价的写寄存器路径」。

**操作步骤**：

1. 看 [main.cc:190-228](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L190-L228) 的 `static void tpu_forward()`——它**不走** `eepif`，而是直接用 `config.h` 的宏 `EEPTPU_BASEADDR0_REG=...; EEPTPU_STARTUP_REG=0x11;`，然后手写 `while((rd_val & 0x80000000) != 0x80000000)` 轮询。
2. 对照 [config.h:29-35](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L29-L35)：这些宏展开后就是 `*(volatile unsigned int*)(0xA0000000+offset)`，与 `eepif.register_write` 编译出的指令**完全相同**。

**预期结果**：两条路径殊途同归——`main.cc::tpu_forward`（宏直写）与 `EEPTPU_SA::forward`（经 `eepif` 抽象）编译后是对同一组寄存器的同一组 store/load。前者更直白、后者更可移植。这解释了为何 u4-l2 说「工程有两套等价实现」。

### 任务三：自己设计一个 register_wait 调用

**实践目标**：巩固 `mask` 与 `want_val` 的含义。

**操作步骤**：假设 TPU 还有一个 2 bit 的「状态码」寄存器在 STATUS 的 bit[2:1]，定义「空闲 = 0b01」。请写出用 `eepreg_wait` 等待它空闲的调用。

**参考答案**：关心 bit[2:1]，故 `mask = 0b110 = 0x6`；期望值为 `0b01`（在 bit[2:1] 位置）= `0b010 = 0x2`。所以调用为 `eepreg_wait(0x0C, 0x00000006, 0x00000002);`。判定式 `(rval & 0x6) == 0x2` 只看 bit[2:1]，忽略其它位。

## 6. 本讲小结

- `EEP_INTERFACE` 是裸机驱动的**最底层抽象**，五个方法（`mem_write/mem_read/register_write/register_read/register_wait`）是所有硬件访问的唯一出口；`EEPTPU_SA` 经成员 `eepif` 调用它，实现「上层管协议、下层管搬字」的解耦，便于跨 AXI/PCIE 后端移植。
- 裸机下物理地址即指针：`register_write` 的本质是 `*(volatile unsigned int*)addr = val;`，`mem_write/read` 是 `volatile unsigned char*` 的逐字节循环拷贝——`volatile` 保证访问不被优化、逐字节保证不对齐地址也安全。
- 上层只传「寄存器偏移」，`eepreg_*` 包装层用 `tpureg_addr | offset` 拼成绝对地址；在本工程里按位或与加法等价，但是一种脆弱写法。
- `register_wait(addr, mask, want_val)` 是通用的「位字段忙等」原语，判据 `(rval & mask) == want_val`；TPU 完成用 `mask=want_val=0x80000000`（等 STATUS bit31 拉高）。它**无超时、无 sleep**，硬件挂起则软件永久死循环。
- `register_read` 的 printf 被注释而 `register_write` 的保留，体现「热路径静默、冷路径可观测」的工程取舍。
- `round_up(x, y)` 是 Linux 内核式位运算对齐宏，要求 `y` 为 2 的幂，被 `epmat_get_size` 用于把通道数向上对齐到 16 的倍数，支撑 TPU 的「16 通道分组」数据格式。

## 7. 下一步学习建议

- 本讲把「写一个寄存器」拆到了指针级，下一讲 **u5-l1（tpu_forward 寄存器时序与完成轮询）** 会把这些寄存器访问**串成完整的时序**：STARTUP=0x11 启动后，软件 `XTime` 计时与硬件 `EEPTPU_RUNTIMER_REG`（偏移 `0x24`）计时如何对照，谁更接近纯 TPU 计算耗时。
- 紧接着 **u5-l2（输出读取与 epmat→ncnn::Mat 转换）** 会把本讲的 `mem_read` 与 `round_up`/`epmat_get_size` 接上 `epmat2nmat`，讲透 16 通道分组、32 字节步长、定点反量化的还原过程。
- 若想横向对照「同一抽象在 Linux 下有两套后端实现」，可回看 **u2-l4（SoC vs PCIE）**：那里的 `EEP_INTERFACE` 在 PCIE 模式下访问的是 `/dev/xdma0_*` 字符设备，而非裸指针——本讲是它的 AXI 单后端简化版。
- 对「为什么裸机需要手动维护缓存一致性」感兴趣的读者，可结合 **u4-l1** 的 `Xil_DCacheDisable/Flush` 反思本讲 `mem_write` 之后为何必须刷缓存。
