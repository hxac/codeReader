# SD 卡网络数据加载与文件读写

## 1. 本讲目标

本讲承接 u4-l1（standalone 工程结构与平台初始化），把裸机工程里那个被 `#ifdef SD_CARD_IS_READY` 包起来的「SD 卡子系统」单独拆出来讲透。学完本讲，你应当能够：

- 说清裸机为什么必须借助 SD 卡来加载网络权重与输入数据；
- 看懂 `sd.c` 里 `SD_Init / file_read / bmp_read / bmp_write / file_write` 五个函数各自的职责与实现细节；
- 解释 `file_read` 如何把一个 `.mem` 文件的字节流直接「灌」进任意 DDR 物理地址；
- 复述 `main.cc` 中 `case '4'` 两条分支（SD 卡读 `eepinput.mem` vs 用编译期烧入的 `eepinput` 数组）的差异，以及 `SD_CARD_IS_READY` 宏如何在这两者间切换；
- 说清为什么每次 `file_read` 之后都必须紧跟一句 `Xil_DCacheFlush()`。

本讲对应的四条最小模块是：SD_Init 初始化、file_read 读 mem 到地址、eepnet/eepinput 加载、bmp_write 保存图像。

## 2. 前置知识

在进入源码前，先建立三个关键直觉。

**(1) 裸机没有文件系统，但 SD 卡可以挂一个。**
在 Linux 路线下，运行库 `libeeptpu_pub` 直接 `load_bin("xxx.pub.bin")` 就能把模型读进内存，因为内核替你管着 ext4/fat 文件系统和块设备驱动。而裸机（standalone）没有操作系统，CPU 一上电就跑你的 `main`，没有 `open/read` 这类 POSIX 调用。ZynqMP 的 Xilinx Standalone BSP 提供了一层 **FatFs**（一个用纯 C 写的、专门给嵌入式用的 FAT 文件系统库），让我们能在 SD 卡上做文件读写——这就是 `sd.c` 里 `f_mount / f_open / f_read / f_write / f_close` 这一组 API 的来源。

**(2) SD 控制器是 DMA 主机，它写内存时绕过 CPU 缓存。**
ZynqMP 的 SD 控制器（SDIO）把卡上的数据搬进 DDR 时，走的是 **DMA**，也就是说数据直接落在 DDR 物理地址上，**不经过 ARM 的数据缓存（D-cache）**。而 TPU 作为另一条 AXI 总线上的主机（见 u1-l4 讲过的 HP 数据通路），它也是直接读 DDR、**完全不感知** ARM 缓存里缓存了什么。于是当一个数据块「由 DMA 写进 DDR、又要被 TPU 读走」时，CPU 缓存里可能还留着这块地址的旧副本（脏行），就会和真实 DDR 内容打架。裸机没有操作系统替你做这种「缓存一致性」维护，必须由程序员手动 `Xil_DCacheFlush()`。这就是本讲反复出现的 `Xil_DCacheFlush()` 的根本原因。

**(3) 内存即物理地址，文件即字节流。**
裸机下，一个 DDR 物理地址（如 `0x31000000`）就是一个可以直接解引用的指针；一个 `.mem` 文件就是一段连续的字节流。`file_read` 做的事，本质上是「把文件字节流原样拷贝到某个物理地址」——既不需要解析，也不需要结构对齐，因为下游（TPU / `eepnet.h` 配置数组）早已约定好这段字节流的二进制含义（详见 u3-l2、u3-l3）。

> 名词速查：**FatFs**（嵌入式 FAT 文件系统库）、**DMA**（直接内存访问，外设与内存间不经过 CPU 的搬运）、**D-cache**（CPU 数据缓存）、**缓存一致性 / cache coherency**（多主机共享内存时各主机看到的数据是否一致）、**BMP**（一种几乎不压缩的位图格式，文件头 54 字节 + 像素数据）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [sdk/standalone/src/sd.c](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c) | FatFs 之上的薄封装，提供 SD 初始化、文件读写、BMP 读写 | `SD_Init / file_read / bmp_write` 三个函数的实现 |
| [sdk/standalone/src/sd.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.h) | 声明上述函数，并定义 `BmpMode` 结构体与三套 BMP 文件头常量 | `BMODE_640x480/1280x720/1920x1080` 的字段含义 |
| [sdk/standalone/src/config.h](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h) | 编译期开关集中地 | `SD_CARD_IS_READY / NET_SIZE / INPUTDATA_SIZE` |
| [sdk/standalone/src/main.cc](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc) | 裸机主程序，调用方 | 启动时加载 `eepnet.mem`、菜单 `case '3'/'4'` 两条使用分支 |
| [sdk/standalone/src/eeptpu/eeptpu_sa.cpp](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp) | `EEPTPU_SA` 类实现（非 SD 分支的对照） | `eeptpu_input` 用 `mem_write` 把输入写到 `hwbase1` |

此外，仓库里实际存放着可直接烧到 SD 卡的成品文件，体积与本讲的尺寸常量逐字节吻合，是很好的验证素材：

- [sdk/standalone/src/net_data/eepnet.mem](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepnet.mem) — 12,240,064 字节 = `NET_SIZE`
- [sdk/standalone/src/net_data/eepinput.mem](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/net_data/eepinput.mem) — 5,537,792 字节 = `INPUTDATA_SIZE`

## 4. 核心概念与源码讲解

### 4.1 SD_Init：挂载 FatFs 文件系统

#### 4.1.1 概念说明

裸机想读 SD 卡上的文件，第一步必须先「挂载文件系统」。`SD_Init()` 就是这第一步：它调用 FatFs 的 `f_mount`，把一个静态的 `FATFS` 对象注册给默认逻辑驱动器，使后续所有 `f_open / f_read / f_write` 都隐式建立在这个挂载之上。挂载本身只做「登记」，真正的底层 SD 卡硬件初始化（SDIO 控制器、卡识别、块设备就绪）由 Xilinx Standalone BSP 的 SD 驱动在首次访问时触发。

#### 4.1.2 核心流程

```
SD_Init()
  ├─ f_mount(&fatfs, "", 0)   # 注册 FATFS 对象到逻辑驱动器 0
  │     ├─ 成功 → 返回 XST_SUCCESS(0)
  │     └─ 失败 → 打印错误码，返回 XST_FAILURE(1)
```

关键点：`fatfs` 是一个**文件级静态变量**（`static FATFS fatfs;`），整个工程的 FatFs 操作共享这唯一一个挂载对象；`""` 表示默认盘符（逻辑驱动器 0，对应 BSP 配置里的那张 SD 卡）；`f_mount` 返回非 0 即失败。

#### 4.1.3 源码精读

静态 `fatfs` 对象与两块行级 scratch 缓冲区定义在文件顶部，三者是整个 `sd.c` 的共享状态：

[sdk/standalone/src/sd.c:L27-L29](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c#L27-L29) — 中文说明：定义全局静态 `FATFS fatfs` 文件系统对象，以及 `read_line_buf` / `Write_line_buf` 两块 `1920*3` 字节的行缓冲（供 BMP 逐行读写，最大支持宽 1920 像素的图像）。

`SD_Init` 的全部实现极其简短：

[sdk/standalone/src/sd.c:L31-L42](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c#L31-L42) — 中文说明：`SD_Init` 调用 `f_mount` 把 `fatfs` 挂到默认驱动器，失败时打印 `rc` 并返回 `XST_FAILURE`，成功返回 `XST_SUCCESS`。

调用方在 `main.cc` 启动序列里，由 `SD_CARD_IS_READY` 宏保护着调用它：

[sdk/standalone/src/main.cc:L264-L267](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L264-L267) — 中文说明：仅在定义了 `SD_CARD_IS_READY` 时才调用 `SD_Init()`，是 SD 子系统的总开关。

#### 4.1.4 代码实践

**实践目标**：理解「关掉 SD 卡」对整个工程的影响。

**操作步骤**（源码阅读型）：

1. 打开 `config.h`，找到 `#define SD_CARD_IS_READY`（约第 64 行）。
2. 在脑中（或用一个注释）把它注释掉，重新通读 `main.cc`。
3. 统计：注释掉后，`main.cc` 里有多少处代码会因此消失（提示：搜索所有 `#ifdef SD_CARD_IS_READY` / `#endif`）。

**需要观察的现象**：

- 启动序列里 `SD_Init()` 调用消失；
- 启动时 `file_read("eepnet.mem", ...)` 整段消失——意味着权重不再从 SD 加载；
- 菜单里 `3: Save Image to SD Card` 选项消失；
- `case '4'` 走入 `#else` 分支（用编译期烧入的 `eepinput` 数组）。

**预期结果**：关掉 `SD_CARD_IS_READY` 会同时切断「权重加载」「输入加载」「存图」三条 SD 数据通路，整个推理链路必须依赖编译期烧入的数据，工程从「可换网络/可换输入」退化为「固化单一输入」。

**待本地验证**：实际编译行为需在 Vitis 工程里验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fatfs` 必须是 `static` 而非局部变量？
**答案**：`fatfs` 要在 `SD_Init` 返回后仍被后续所有 `f_open/f_read` 引用——FatFs 内部通过挂载时传入的指针长期持有它。若它是局部变量，函数返回后栈帧销毁，指针悬空，文件操作会读写到随机内存。

**练习 2**：`SD_Init` 失败时返回 `XST_FAILURE`，但 `main.cc` 里 `SD_Init()` 的返回值并没有被检查。这会带来什么隐患？
**答案**：若 SD 卡没插好或硬件未就绪，`f_mount` 失败，但程序继续往下跑；随后启动序列的 `file_read("eepnet.mem", ...)` 也会失败（返回 `XST_FAILURE`），而那里同样没检查返回值，于是 `eepnet` 指向的内存里仍是随机数据，TPU 拿到的就是「垃圾权重」。工程依赖开发者保证 SD 卡物理就绪。

---

### 4.2 file_read：把 mem 文件搬到任意物理地址

#### 4.2.1 概念说明

`file_read` 是裸机 SD 子系统中最核心、也最「裸」的函数。它把一个文件的全部内容，原样拷贝到一个**你指定的物理地址**上——不做任何解析、不做格式转换、不做地址对齐修正。这种「文件 → 物理地址」的直通能力，正是裸机能把 `eepnet.mem`（网络权重）和 `eepinput.mem`（硬件输入）直接灌进 TPU 数据内存区（`hwbase0 / hwbase1`）的关键。

#### 4.2.2 核心流程

```
file_read(path, frame, len)
  ├─ f_open(&fil, path, FA_OPEN_EXISTING | FA_READ)   # 只读打开
  │     └─ 失败 → 打印 "open file fail!"，返回 XST_FAILURE
  ├─ f_read(&fil, (void*)frame, len, &br)              # 把 len 字节读进 frame
  │     └─ 失败 → 打印 "read file fail!"，返回 XST_FAILURE
  ├─ f_close(&fil)                                      # 关闭文件
  └─ 返回 XST_SUCCESS
```

注意第二个参数 `frame` 被直接强转为 `void*` 喂给 `f_read` 的缓冲区指针——也就是说 `frame` 既可以是堆指针，也可以是裸 DDR 物理地址（如 `(u8*)0x31000000`）。对 FatFs 来说，它只是「往这个地址写字节」，根本不在乎那是普通内存还是外设映射内存。

读多少字节由第三个参数 `len` 决定，调用方必须保证 `len` 与文件实际大小匹配，且 `frame` 指向的区域足够大——`file_read` 自身不做任何越界检查。

#### 4.2.3 源码精读

`file_read` 的完整实现：

[sdk/standalone/src/sd.c:L128-L151](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c#L128-L151) — 中文说明：`file_read` 依次 `f_open`（只读）→ `f_read`（把 `len` 字节读入 `frame`）→ `f_close`，任一步失败即打印错误并返回失败码。

关键的那一行：

[sdk/standalone/src/sd.c:L142](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c#L142) — 中文说明：`f_read(&fil, (void*)frame, len, &br)` 是整个函数的实质——把文件 `len` 字节直接读入调用方传入的任意地址 `frame`；`br` 回带实际读取的字节数。

对照 `sd.h` 里的声明，可见 `frame` 是 `unsigned char*`、`len` 是 `unsigned int`：

[sdk/standalone/src/sd.h:L99-L103](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.h#L99-L103) — 中文说明：声明 `SD_Init / bmp_read / bmp_write / file_read / file_write` 五个对外函数的原型，`file_read` 接收「路径 + 目标地址 + 长度」三参数。

#### 4.2.4 代码实践

**实践目标**：体会「文件即字节流、内存即物理地址」的直通语义。

**操作步骤**（源码阅读 + 手算型）：

1. 在 `main.cc` 启动序列找到这行：
   `file_read("eepnet.mem",eepnet, NET_SIZE);`
2. 追踪 `eepnet` 的来源：它在 `main.cc` 里被赋值为 `eepnet = (u8 *)waddr;`，而 `waddr = eepsa.hwbase0`。
3. 回顾 u3-l3 / u4-l2：`hwbase0` 是 `eepnet_config` 数组里解析出的「par 段」DDR 基址。
4. 计算：`eepnet.mem` 在仓库里实测是 12,240,064 字节，而 `config.h` 里 `NET_SIZE` 是多少？

**需要观察的现象**：磁盘上 `.mem` 文件大小与 `config.h` 的 `NET_SIZE` 是否完全相等。

**预期结果**：两者完全相等（12,240,064 == `NET_SIZE`），证明 `file_read` 是「整文件一次性读入」、`len` 必须精确等于文件体积；若换网络导致权重体积变化，必须同步改 `config.h` 的 `NET_SIZE`，否则会读不完整或读越界。

**待本地验证**：换网络后的实际字节数需重新编译生成 `.mem` 后核对。

#### 4.2.5 小练习与答案

**练习 1**：如果 `len` 比文件实际大小大，`f_read` 会发生什么？`frame` 后面那段会是什么内容？
**答案**：`f_read` 读到文件尾后停止，`br` 回带实际读到的字节数（小于 `len`），`frame` 中 `[br, len)` 这段保持原样未被写入（本实现没有 `memset` 清零，所以是调用前残留的内容或随机值）。`file_read` 没有校验 `br == len`，故「读不完整」会被静默吞掉。

**练习 2**：为什么 `file_read` 适合加载 `.mem`，却不适合加载 `.bmp`？
**答案**：`.mem` 是纯数据字节流，直接落到目标地址即可；`.bmp` 头部是 54 字节的文件头 + 像素数据，且像素是「自下而上、行内可能 4 字节对齐」存储的，需要专门的 `bmp_read`（见 `sd.c` 同文件）解析头部并做行翻转，不能整块直拷。

---

### 4.3 eepnet/eepinput 加载与 SD_CARD_IS_READY 编译开关

#### 4.3.1 概念说明

本模块把前两个模块串起来，落到 `main.cc` 的真实使用场景。裸机推理需要两类 SD 数据：

1. **网络权重 `eepnet.mem`**（约 12 MB）：在启动序列里**一次性**加载到 `hwbase0`（TPU 的 par/in 段，权重 + 调度表所在 DDR 区）。
2. **硬件输入 `eepinput.mem`**（约 5.3 MB）：一张样例图经 resize/mean/norm/定点/32 字节打包后的硬件张量（详见 u4-l4），在菜单 `case '4'` 里加载到 `hwbase1`（TPU 的输入段）。

`SD_CARD_IS_READY` 是 `config.h` 里的编译期开关，它用条件编译在两条「输入来源」之间切换：开 → 从 SD 读 `eepinput.mem`；关 → 用编译期烧入 ELF 的 `eepinput` 数组（经 `eeptpu_input` 写入）。这正是本讲规格里要讲透的对比点。

#### 4.3.2 核心流程

```
启动序列（main.cc）:
  eeptpu_init(...) 解析 eepnet_config → 得到 hwbase0/hwbase1 等地址
  #ifdef SD_CARD_IS_READY
      file_read("eepnet.mem", eepnet=hwbase0, NET_SIZE)   # 灌权重
      Xil_DCacheFlush()                                   # 刷新缓存
  #endif

菜单 case '4'（加载输入）:
  #ifdef SD_CARD_IS_READY
      file_read("eepinput.mem", eepinput_addr=hwbase1, INPUTDATA_SIZE)  # 灌输入
      Xil_DCacheFlush()
  #else
      eepsa.eeptpu_input(eepinput, sizeof(eepinput))      # 把数组写入 hwbase1
  #endif
```

输入尺寸的来源是一道漂亮的算术。回顾 u4-l4，硬件输入按「16 通道 × 2 字节 = 32 字节」为最小访存单元打包，每个空间位置独占一个 32 字节槽位。对 416×416×3 的网络输入，打包后的字节数为：

\[
\text{size} = W \times H \times 32 = 416 \times 416 \times 32 = 5\,537\,792 \;\text{字节}
\]

这恰好等于 `config.h` 的 `INPUTDATA_SIZE`，也等于磁盘上 `eepinput.mem` 的实际大小——三者逐字节吻合，是验证「SD 加载—内存布局—配置常量」三者一致的硬证据。

#### 4.3.3 源码精读

启动序列里加载网络权重的整段：

[sdk/standalone/src/main.cc:L311-L317](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L311-L317) — 中文说明：在 `SD_CARD_IS_READY` 下，把 `eepnet.mem`（`NET_SIZE` 字节）读入 `eepnet`（即 `hwbase0`），紧接着 `Xil_DCacheFlush()`。

地址变量的赋值来源（理解 `eepnet` / `eepinput_addr` 指向哪里）：

[sdk/standalone/src/main.cc:L298-L304](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L298-L304) — 中文说明：把 `eeptpu_init` 解析出的 `hwbase0/hwbase1` 等地址拷给全局变量 `waddr/sd_input_addr`，并令 `eepinput_addr = (u8*)sd_input_addr`、`eepnet = (u8*)waddr`，使后续 `file_read` 的目标地址与 TPU 数据段对齐。

本模块的核心——`case '4'` 的两条分支：

[sdk/standalone/src/main.cc:L514-L527](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L514-L527) — 中文说明：`case '4'` 在 `SD_CARD_IS_READY` 时用 `file_read("eepinput.mem", eepinput_addr, INPUTDATA_SIZE)` 从 SD 读输入并 `Xil_DCacheFlush()`；否则用 `eepsa.eeptpu_input(eepinput, sizeof(eepinput))` 把编译期烧入的 `eepinput` 数组写入 TPU。

非 SD 分支里 `eeptpu_input` 的实现，揭示了它同样写入 `hwbase1`：

[sdk/standalone/src/eeptpu/eeptpu_sa.cpp:L218-L226](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L218-L226) — 中文说明：`eeptpu_input` 调用 `eepif.mem_write(hwbase1, datalen, input_data, datalen)`，把 `input_data`（即 `eepinput` 数组）经 AXI 数据通路写入输入段 `hwbase1`——与 SD 分支最终落点完全相同。

最后，`config.h` 里的尺寸常量与开关：

[sdk/standalone/src/config.h:L44-L47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L44-L47) — 中文说明：`NET_SIZE = 12240064`、`INPUTDATA_SIZE = 5537792`，分别约束 `eepnet.mem` 与 `eepinput.mem` 的读取长度。

[sdk/standalone/src/config.h:L63-L64](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/config.h#L63-L64) — 中文说明：`#define SD_CARD_IS_READY` 是 SD 子系统的总开关。

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：对比 `case '4'` 的两条分支，说清 `SD_CARD_IS_READY` 如何切换输入来源，以及为何读完后要 `Xil_DCacheFlush()`。

**操作步骤**（源码阅读 + 推理型）：

1. 打开 `main.cc` 的 `case '4'`（约第 514–527 行），并列对照两条分支：

   | | SD 卡就绪分支 | 非 SD 分支 |
   | --- | --- | --- |
   | 数据来源 | SD 卡上的 `eepinput.mem` 文件 | 编译期烧入 ELF 的 `eepinput` C 数组 |
   | 搬运手段 | `file_read(...)`（FatFs + SD DMA） | `eepsa.eeptpu_input(...)`（底层 `eepif.mem_write`） |
   | 落点地址 | `eepinput_addr` = `hwbase1` | `hwbase1`（在 `eeptpu_input` 内部） |
   | 后处理 | `Xil_DCacheFlush()` | 无显式 flush |

2. 解释 `SD_CARD_IS_READY` 的切换机制：它是**编译期**宏，`#ifdef` 在预处理阶段就把另一条分支整段剔除（死代码），不存在运行时 if/else——这呼应了 u2-l4 讲过的「条件编译 vs 运行时变量」两种切换风格。
3. 解释 `Xil_DCacheFlush()` 的必要性（见下方「需要观察的现象」中的推理）。

**需要观察的现象 / 关键推理**：

- **两条分支殊途同归**：无论哪种来源，最终都要把「一帧硬件输入」写进 TPU 的输入内存段 `hwbase1`，因为 TPU forward 时只会去 `hwbase1` 取数。SD 分支靠 DMA 把文件字节直接落在 `hwbase1` 上；非 SD 分支靠 CPU 经 AXI `mem_write` 把数组拷过去。落点相同，只是搬运工不同。
- **为何读完后必须 `Xil_DCacheFlush()`**：这是本实践的核心考点。SD 控制器是 **DMA 主机**，它把文件字节直接写进 DDR 物理地址 `hwbase1`，**绕过 ARM 数据缓存**；而 TPU 是另一条 AXI 总线上的主机（走 HP 口，见 u1-l4），它读 DDR 时**完全不感知** ARM 缓存里缓存了什么。于是存在一种危险：ARM 缓存里若残留了 `hwbase1` 这块地址的**旧脏行**，等到 TPU 取数时，DDR 里已被 DMA 刷新成新数据，但 ARM 缓存的脏行可能随后回写、把新数据覆盖回旧值；或者反过来，TPU 看到的不是 DMA 刚写入的值。裸机没有操作系统代管这种「多主机缓存一致性」，所以必须由程序员在「DMA 写完」与「TPU 读」之间手动 `Xil_DCacheFlush()` 把脏行写回、让缓存与 DDR 一致。

  > 旁注：`main.cc` 在第 279 行其实已经 `Xil_DCacheDisable()` 全局关掉了 D-cache（缓存关闭时 CPU 直读 DDR，多数一致性问题自动消失）。但工程仍保留 `file_read` 后的显式 `Xil_DCacheFlush()`，作为「DMA + 独立总线主机」场景下的防御性正确性保证——尤其考虑到 `case '2'` 的 yolo3 CPU 后处理会临时 `Xil_DCacheEnable/Flush/Disable` 反复切换缓存状态（见 main.cc 第 416–422 行），缓存并非全程关闭。`Xil_DCacheFlush()` 在缓存已关时基本是空操作、无副作用，因此「宁可多 flush」是稳妥的工程选择。

**预期结果**：能复述「`SD_CARD_IS_READY` 是编译期剔除式开关；两分支最终都写 `hwbase1`；DMA 写入绕过 CPU 缓存、而 TPU 另走总线读 DDR、二者无硬件一致性，故须手动 flush」这一完整因果链。

**待本地验证**：在真机上若故意去掉 `Xil_DCacheFlush()`、并临时开启 D-cache，理论上会偶发推理结果错乱——属硬件级实验，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：假如换了一个更大的网络，`eepnet.mem` 变成 20 MB，需要同步改哪些地方？
**答案**：(1) `config.h` 的 `NET_SIZE` 改成新的字节数；(2) 确认 `hwbase0` 所在的 DDR 区段（`EEPTPU_MEM_BASE_ADDR` 起）有足够余量，且不与输入/输出/临时段重叠；(3) SD 卡上替换新的 `eepnet.mem`。`file_read` 用 `NET_SIZE` 决定读取长度，不改它就会读不完整。

**练习 2**：为什么 `eeptpu_input` 分支（非 SD）之后**没有** `Xil_DCacheFlush()`？
**答案**：`eeptpu_input` 走的是 CPU 主动的 AXI `mem_write`（CPU 是写入发起方），不是 SD DMA；且 `main.cc` 全局已 `Xil_DCacheDisable()`。即便如此，写完后 TPU 读到的也已经是 DDR 实际内容。SD 分支之所以多一句 flush，正是因为它是 **DMA 写入**（CPU 不是发起方），需要显式做缓存同步。

---

### 4.4 bmp_write：把采集图像存成 BMP

#### 4.4.1 概念说明

推理结果除了在串口打印、在 DP 屏幕显示，还可以**存成图片**留档——这就是菜单 `case '3'`（Save Image to SD Card）做的事，由 `bmp_write` 实现。BMP 是一种几乎不压缩的位图格式：开头 54 字节文件头（14 字节文件头 + 40 字节信息头），随后是**自下而上、逐行**排列的像素数据，每像素 3 字节（BGR）。`bmp_write` 的任务就是把内存里「自上而下」排布的帧缓冲，翻转成 BMP 要求的「自下而上」顺序写入文件。

#### 4.4.2 核心流程

```
bmp_write(name, head_buf, data_buf)
  ├─ memset(Write_line_buf, 0, 1920*3)            # 清行缓冲
  ├─ f_open(&fil, name, FA_CREATE_ALWAYS|FA_WRITE) # 新建/覆盖写
  ├─ f_write(&fil, head_buf, 54, &br)              # 先写 54 字节文件头
  ├─ 从 head_buf 解析宽 Ximage、高 Yimage
  ├─ iPixelAddr = (Yimage-1) * Ximage * 3          # 从最后一行(图像底部)开始
  ├─ for y in [0, Yimage):
  │     ├─ for x in [0, Ximage): 逐像素从 data_buf 行拷到 Write_line_buf
  │     ├─ f_write(&fil, Write_line_buf, Ximage*3)  # 写一整行
  │     └─ iPixelAddr -= Ximage*3                    # 上移一行
  └─ f_close(&fil)
```

BMP 文件总长（不含对齐时）为：

\[
\text{filelen} = W \times H \times 3 + 54
\]

例如 640×480×24bit：\(640 \times 480 \times 3 = 921\,600\)，加 54 字节头 = 921,654 字节，这与 `sd.h` 里 `BMODE_640x480` 注释的「921600+54 bytes」一致。

#### 4.4.3 源码精读

`bmp_write` 的翻转写入逻辑：

[sdk/standalone/src/sd.c:L84-L126](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c#L84-L126) — 中文说明：`bmp_write` 先写 54 字节头，再从 `data_buf` 的最后一行开始、逐行（每行逐像素 BGR）拷到行缓冲并写入文件，实现「内存自上而下 → BMP 自下而上」的翻转。

关键的两行地址数学：

[sdk/standalone/src/sd.c:L108](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c#L108) — 中文说明：`iPixelAddr = (Yimage-1)*Ximage*3`，把读指针定位到 `data_buf` 的**最后一行**（图像底部），是「自下而上」翻转的起点。

[sdk/standalone/src/sd.c:L122](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.c#L122) — 中文说明：每写完一行，`iPixelAddr -= Ximage*3`，把读指针**上移一行**，循环 Yimage 次即完成整幅翻转。

文件头从哪来——`sd.h` 里预定义的三套 BMP 头常量，分别对应三种分辨率：

[sdk/standalone/src/sd.h:L45-L97](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/sd.h#L45-L97) — 中文说明：`BMODE_640x480 / BMODE_1280x720 / BMODE_1920x1080` 三套 `BmpMode` 常量，每套都是一段硬编码的 54 字节 BMP 头（含魔数 `0x42 0x4d`="BM"、宽、高、24bit 真彩色、不压缩等字段）。

调用方按当前采集分辨率选用对应文件头：

[sdk/standalone/src/main.cc:L500-L513](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/main.cc#L500-L513) — 中文说明：`case '3'` 按 `pic_vsize`（480/720/1080）选用对应的 `BMODE_*` 头，把 `img_data_888`（RGB888 帧缓冲）存成 `pic_<n>.bmp`。

> 注意 `BmpMode` 用 `char[]` 数组逐字节存放各字段（而非 `uint16/uint32`），是为了与 BMP 文件的「小端序、紧凑布局」逐字节对应，省去结构体对齐带来的填充问题。

#### 4.4.4 代码实践

**实践目标**：理解 BMP「自下而上」存储与分辨率→文件头的对应关系。

**操作步骤**（源码阅读 + 手算型）：

1. 读 `BMODE_1280x720`（`sd.h` 第 63–79 行），手算验证：
   - `.bm_width = {0x00, 0x05, 0x00, 0x00}` → 小端 uint32 = `0x00000500` = **1280**；
   - `.bm_height = {0xd0, 0x02, 0x00, 0x00}` → `0x000002d0` = **720**；
   - `.pixel_width = {0x18, 0x00}` → `0x0018` = **24 bit**。
2. 计算 1280×720×24bit BMP 的像素数据字节数：\(1280 \times 720 \times 3 = 2\,764\,800\) 字节。
3. 在 `main.cc` 的 `case '3'` 里确认：只有 `pic_vsize` 等于 480/720/1080 之一才会存图，否则打印 `Unsupport resolution!!!`。

**需要观察的现象**：`config.h` 里 `IMG_HEIGHT 720 / IMG_WIDTH 1280`（约第 60–61 行），故默认运行时 `pic_vsize == 720`，`case '3'` 会走 `BMODE_1280x720` 分支。

**预期结果**：能从硬编码字节数组反解出分辨率，并明白「换分辨率必须同步加一套 `BMODE_*` 头并在 `case '3'` 加一个 `else if` 分支」。

**待本地验证**：实际生成的 `pic_1.bmp` 能否在 PC 上正常打开，需上板验证。

#### 4.4.5 小练习与答案

**练习 1**：`bmp_write` 为什么要先写文件头、再按行翻转写像素，而不是把整块 `data_buf` 一次性 `f_write`？
**答案**：因为 BMP 规定像素「自下而上」存储，而 `data_buf`（帧缓冲）是「自上而下」的内存布局。一次性写入会得到上下颠倒的图。逐行从最后一行往第一行拷，正是为了完成这个垂直翻转。

**练习 2**：若要支持 800×600 分辨率存图，需要改哪些地方？
**答案**：(1) 在 `sd.h` 新增一套 `BMODE_800x600` 常量（手算 bm_len/bm_bytes = 800×600×3+54、bm_width=800、bm_height=600）；(2) 在 `main.cc` 的 `case '3'` 里加 `else if (pic_vsize == 600) bmp_write(pic_name, (char*)&BMODE_800x600, (char*)img_data_888);`。

---

## 5. 综合实践

把本讲四个模块串成一个端到端的小任务：**画出「裸机推理的 SD 数据供给链」全图，并标注每一步对应的源码位置与缓存维护点**。

请用文字或伪流程图回答下列问题：

1. **启动阶段**：从 `main()` 进入到菜单出现之前，SD 相关的两次关键操作分别是什么？各把数据写到哪个物理地址？对应的源码行是哪些？
   - 提示：`SD_Init()`（main.cc 第 266 行）、`file_read("eepnet.mem", eepnet, NET_SIZE)` + `Xil_DCacheFlush()`（main.cc 第 314–315 行）；目标地址 `eepnet = hwbase0`。
2. **运行阶段**：用户在串口输入 `4`（Read Test Image），若 `SD_CARD_IS_READY` 已定义，会发生什么？若未定义呢？两条分支的共同落点是什么？
   - 提示：落点都是 TPU 输入段 `hwbase1`（= `eepinput_addr` = `sd_input_addr`）。
3. **存档阶段**：用户输入 `3`（Save Image），`bmp_write` 如何把 `img_data_888` 翻转成 BMP？为什么必须按分辨率选不同的 `BMODE_*` 头？
4. **缓存维护**：在上面三个阶段里，哪几处调用了 `Xil_DCacheFlush()`？为什么唯独「SD 读文件」之后需要、而「`eeptpu_input` 数组写入」之后不需要？

**交付物**：一张含「数据源 → 搬运函数 → 目标 DDR 地址 → 下游消费者（TPU / PC）」四列的表格，并在「SD DMA 写入」的格子旁注明「必须 `Xil_DCacheFlush()`，因为 TPU 另走总线读 DDR、无硬件缓存一致性」。

**待本地验证**：上板跑通菜单 `1→2`（采集+forward）与 `3`（存图），核对 SD 卡上是否出现 `pic_1.bmp`、串口是否打印 `forward time is ... us`。

## 6. 本讲小结

- `sd.c` 是一层 FatFs 薄封装，提供 `SD_Init / file_read / file_write / bmp_read / bmp_write`，把「SD 卡上的文件」与「DDR 物理地址」打通。
- `SD_Init` 只做一件事：`f_mount` 注册全局静态 `FATFS` 对象，后续所有文件操作共享它。
- `file_read(path, frame, len)` 的本质是「把文件 `len` 字节原样灌进任意物理地址 `frame`」，不做解析、不做对齐，是裸机能把 `.mem` 直送 TPU 数据段的关键；`len` 必须精确等于文件大小（`NET_SIZE`/`INPUTDATA_SIZE` 与磁盘 `.mem` 逐字节吻合即证）。
- `eepnet.mem`→`hwbase0`（权重）在启动序列加载一次；`eepinput.mem`→`hwbase1`（输入）在 `case '4'` 加载；`SD_CARD_IS_READY` 是编译期剔除式开关，在「SD 读文件」与「`eeptpu_input` 写数组」两套输入来源间切换，两分支最终落点都是 `hwbase1`。
- 每次 `file_read` 之后必须 `Xil_DCacheFlush()`：SD 控制器是 DMA 主机、写 DDR 绕过 CPU 缓存，而 TPU 另走 AXI/HP 总线读 DDR、无硬件一致性，裸机无 OS 代管，须手动同步。
- `bmp_write` 把内存自上而下的帧缓冲翻转成 BMP 自下而上的存储顺序，按分辨率选用预定义的 `BMODE_*` 文件头（54 字节）。

## 7. 下一步学习建议

本讲把 SD 子系统讲透后，裸机的「数据输入」侧已完整。建议继续：

- **横向对照 Linux 路线**：回到 u2-l3 / u2-l4，体会 Linux 下 `load_bin` 一行搞定的事，裸机为何要拆成 `eeptpu_init` + `file_read` + `Xil_DCacheFlush` 三步——加深对「有无操作系统」差异的理解。
- **向下游延伸**：读 u5-l1（tpu_forward 寄存器时序）和 u5-l2（输出读取与 epmat 反量化），看 `hwbase0/hwbase1` 上的数据是如何被 TPU 消费、又如何把结果读回的，形成「输入 → 计算 → 输出」完整闭环。
- **向平台底层延伸**：结合 u8-l1（DVP 摄像头与 DP 显示链路），把本讲的 SD、第 8 单元的摄像头/显示、以及 u4-l1 的中断初始化拼成一张完整的「裸机外设数据流图」。
- **移植实战**：参考 u8-l4（性能、精度与移植实践），尝试把 `eepnet.mem/eepinput.mem` 换成自己编译的网络，练习「改 `config.h` 尺寸常量 → 替换 `.mem` → 重新烧卡」的完整换模型流程。
