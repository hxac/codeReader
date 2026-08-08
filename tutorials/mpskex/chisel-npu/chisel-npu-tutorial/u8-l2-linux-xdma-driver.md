# Linux XDMA 内核驱动与 C 工具

## 1. 本讲目标

u8-l1 把你带到了 FPGA 的「门口」：数据通路是「主机 PCIe → XDMA IP → 各级 AXI 桥 → MIG → 双片 DDR3」，并且 V10 平台把 `ctrl_lite + npu_dma_master + MMALU` 装进了单一的 `npu_subsys`，挂入统一的 4 GB 地址空间。但主机到底用**什么软件**去写 DDR3、又用**什么命令**去戳那一位 `start` 把 NPU 跑起来，u8-l1 留给了本讲。

本讲讲解 Xilinx XDMA 内核驱动（`xdma.ko`）及其配套的 C 用户态工具，学完之后你应当能够：

1. 说清 `xdma.ko` 加载后在 `/dev/` 下创建的三类字符设备节点（`h2c` / `c2h` / `bypass`/`user`）各自承担什么角色。
2. 区分**两条完全不同的访问路径**：`reg_rw` 走 `mmap` + 指针解引用做内存映射寄存器（MMIO）单字读写；`dma_to_device` / `dma_from_device` 走 `write()`/`read()` + `lseek` 触发内核的散列-聚集 DMA（SGDMA）做批量搬运。
3. 读懂 `ctrl_lite` 的「`start`/`done`/`busy`」三比特控制协议，并用 `reg_rw` 在 bypass BAR 上 kick 一次 NPU、轮询完成。
4. 把硬件侧 `npu_dma_master` 的状态机（staging A/B/ACCUM → kick → wait `io_clct` → write OUT）翻译成主机侧 C 工具的命令序列，并解释这条序列为何与 `ctrl_lite + DMA staging` 协议严格一致。

本讲是 U8 的中间一讲：u8-l1 给了硬件拓扑，本讲给出**主机到硬件的软件接口契约**，u8-l3（Python 用户态驱动）会在这个契约之上再包一层「numpy 优先、地址不可见」的 pybind11 边界。三者共享同一个 `ctrl_lite + DMA staging` 协议。

## 2. 前置知识

本讲假定你已学过 **u8-l1（FPGA 验证平台 xc7k480t）**，并且知道：

- 数据通路里 XDMA IP 把 PCIe 事务转成 128 位 AXI4，再经时钟/位宽转换与交叉开关落到 MIG 与 DDR3。
- V10 用单一 `npu_subsys`（`ctrl_lite` + `npu_dma_master` + MMALU）挂进统一 4 GB 地址空间；主机只要「把数据写到 DDR3 的固定区域 + 向 `ctrl_lite` 写一比特 `start`」，`npu_dma_master` 就会自主完成「读 A/B/ACCUM → 喂给 MMALU → 等结果 → 把 OUT 写回 DDR3」。

同时建议你带着 **u4-l5（MMALU 顶层集成）** 里的两个事实：MMALU 的 `io_ctrl_keep`/`io_ctrl_use_accum` 控制位，以及 `io_clct` 是「整批完成」标志。本讲你会看到 `npu_dma_master` 如何驱动这几个端口。

下面先补几个操作系统与总线的术语：

- **字符设备（character device）**：Linux 里按字节流访问的设备文件，`/dev/xdma0_*` 就是这一类。用户态程序用 `open`/`read`/`write`/`ioctl`/`mmap` 这些标准系统调用与它交互，内核驱动在背后把这些调用翻译成对硬件的操作。
- **内核模块（kernel module，`.ko`）**：可以热插拔地加载进运行中的内核的代码段。`xdma.ko` 就是 Xilinx 提供的 PCIe DMA 驱动，`insmod xdma.ko` 把它装进内核，它就认领板卡并创建 `/dev/xdma0_*` 设备节点。
- **BAR（Base Address Register）**：PCIe 设备向主机暴露的一段「地址窗口」。主机往 BAR 窗口里写一个地址，PCIe 控制器就把它翻译成对设备内部寄存器/存储的访问。XDMA 通常把一个 BAR 配成「AXI-Lite bypass」窗口，让主机能直接 PIO 访问片上 AXI-Lite 从设备——`ctrl_lite` 就挂在这个窗口后面。
- **SGDMA（Scatter-Gather DMA）**：主机把一段（或几段）内存缓冲描述符交给 DMA 引擎，引擎自行把数据搬进/搬出设备，搬完用中断（或轮询）通知主机。对比 `reg_rw` 的「单字 PIO」，SGDMA 是「批量、由硬件搬运」。
- **PIO（Programmed I/O）与 MMIO（Memory-Mapped I/O）**：CPU 用一条访存指令直接读写设备寄存器。`reg_rw` 把 BAR 设备 `mmap` 进进程地址空间后，对一个普通指针解引用就是一次 MMIO。

> 关于「诚实边界」：本讲涉及的 C 工具（`reg_rw`、`dma_to_device`、`dma_from_device`、`dma_utils`、`performance`）都是 **Xilinx 官方的 vendor 工具**，源码在 `driver/linux/tools/` 与 `driver/linux/xdma/`，仓库只是做了源码镜像（见 `driver/linux/README.md`）。NPU 特有的东西只有两处：硬件侧的 `ctrl_lite` 寄存器映射（`npu_ctrl_lite.v`）与 `npu_dma_master` 的 DDR staging 地址表（`npu_dma_master.v`）。本讲会把 vendor 工具的通用机制讲清楚，再把这两处 NPU 契约接上去。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|:-----|:-----|
| [driver/linux/README.md](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/README.md) | XDMA 驱动源码镜像说明：目录布局、编辑后 rsync 到 FPGA 机、远程编译、`reboot_and_load.sh` 重新加载的部署工作流。 |
| [driver/linux/tools/reg_rw.c](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c) | BAR 寄存器读写工具：`mmap` 字符设备 + 指针解引用，支持 byte/halfword/word 三种宽度，读或写由参数个数决定。 |
| [driver/linux/tools/dma_to_device.c](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c) | 主机→板卡（H2C）SGDMA 工具：默认设备 `/dev/xdma0_h2c_0`，用 `write()` + `lseek(AXI 地址)` 把缓冲写到 DDR3。 |
| [driver/linux/tools/dma_from_device.c](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_from_device.c) | 板卡→主机（C2H）SGDMA 工具：默认设备 `/dev/xdma0_c2h_0`，结构是 `dma_to_device` 的对偶。 |
| [driver/linux/tools/dma_utils.c](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_utils.c) | 两个 DMA 工具共享的工具函数：`write_from_buffer`/`read_to_buffer`，关键在于用 `lseek` 把 AXI 地址设为文件偏移。 |
| [ip/vivado/xc7k480t/src/npu_ctrl_lite.v](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v) | NPU 控制寄存器（AXI4-Lite 从设备）：定义 `start`/`done`/`busy` 三比特协议，由主机经 bypass BAR 访问。 |
| [ip/vivado/xc7k480t/src/npu_dma_master.v](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v) | NPU 的 AXI4 master 数据搬运状态机：定义 A/B/ACCUM/OUT 在 DDR3 的固定 staging 地址，kick 后自主完成一整轮 MMALU 计算。 |
| [tool/hw/tests/lib/xdma.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py) | SSH 远程封装：把 `reg_rw`/`dma_to_device`/`dma_from_device` 三条 C 工具命令行原样记录下来，是本讲「C 工具如何被编排」的活证据。 |
| [tool/hw/tests/lib/reg_rw.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/reg_rw.py) | `ctrl_lite` 的高层封装：`kick_start`/`wait_done`/`is_busy`/`is_done`，把三比特协议映射成 `reg_rw` 命令。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先认识设备节点（4.1），再分别拆开两条访问路径——寄存器级的 `reg_rw`（4.2）与数据级的 `dma_to_device`/`dma_from_device`（4.3），最后把 `ctrl_lite` 协议与硬件状态机接上，拼出一次完整 MMALU 计算的 C 工具序列（4.4）。

### 4.1 XDMA 设备节点与三类通道

#### 4.1.1 概念说明

`xdma.ko` 加载后会为板卡创建一组字符设备节点，集中在 `/dev/xdma0_*`（多卡时还会有 `/dev/xdma/card*`）。理解本讲，你只需记住三类通道：

1. **H2C（Host-to-Card）通道** `/dev/xdma0_h2c_0`、`_h2c_1` …：主机向板卡 DDR 写数据的「出口」，对应 XDMA 的 AXI4 master 读通道。主机在这类设备上 `write()`，数据就经 SGDMA 落到 DDR3。
2. **C2H（Card-to-Host）通道** `/dev/xdma0_c2h_0`、`_c2h_1` …：板卡向主机读数据的「入口」，是 H2C 的对偶。主机 `read()`，数据从 DDR3 搬回主机缓冲。
3. **控制/BAR 通道** `/dev/xdma0_bypass`（或 `/dev/xdma0_user`）：暴露 XDMA 的 AXI-Lite bypass 窗口，主机用它做**单字 PIO 寄存器访问**。`ctrl_lite` 就挂在这个窗口后面，所以 kick/poll 都走这一类设备。

`driver/linux/readme.txt` 把这些脚本与设备节点的关系讲得很清楚：`load_driver.sh` 负责加载内核模块并「创建必要的内核节点」，节点落在 `/dev/xdma*` 下。

一个关键区分：H2C/C2H 走的是**高速 SGDMA 数据通路**，位宽 128 位、突发传输、吞吐优先；bypass/user 走的是**轻量 AXI-Lite 控制通路**，32 位、单字、用来戳控制/状态寄存器。本讲的两个工具 `reg_rw` 与 `dma_*` 恰好分别对应这两条通路。

#### 4.1.2 核心流程

设备节点从无到有、再到被工具打开的流程：

```
insmod xdma.ko  ──►  内核 probe PCIe 设备  ──►  创建 /dev/xdma0_{h2c,c2h,bypass,...}
        │                                                  │
   load_driver.sh                                    用户态工具 open() 对应节点
   (driver/linux/tests/)                                    │
                                                  驱动把系统调用翻译成 AXI 事务
```

- 加载：`driver/linux/tests/load_driver.sh` 先 `rmmod xdma`（若已加载），再 `insmod ../xdma/xdma.ko`（可选中断模式参数 `interrupt_mode=` 或 `poll_mode=1`），最后 `cat /proc/devices | grep xdma` 确认设备被认到。
- 识别板卡：驱动靠 PCI 设备 ID 认领板卡，`driver/linux/readme.txt` 指出 ID 表在 `xdma/xdma_mod.c` 的 `pci_device_id` 结构里，形如 `{ PCI_DEVICE(0x10ee, 0x8038), }`。
- 打开：每个用户态工具第一步都是 `open("/dev/xdma0_*", ...)`，拿到 `fd` 后用 `read`/`write`/`ioctl`/`mmap` 之一操作。

#### 4.1.3 源码精读

设备节点是 `xdma.ko` 内核模块创建的，不在本讲的 C 工具源码里；但工具默认设备名把这条对应关系钉死了：

- `dma_to_device.c` 把默认设备设为 H2C 通道 0：[driver/linux/tools/dma_to_device.c:L45-L47](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L45-L47) 定义 `DEVICE_NAME_DEFAULT "/dev/xdma0_h2c_0"`、`SIZE_DEFAULT (32)`、`COUNT_DEFAULT (1)`——即「不传参就往 H2C 通道 0 写 32 字节、写 1 次」。

- `dma_from_device.c` 的默认设备是 C2H 通道 0：`#define DEVICE_NAME_DEFAULT "/dev/xdma0_c2h_0"`（[driver/linux/tools/dma_from_device.c:L30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_from_device.c#L30)），与 `dma_to_device` 完全对偶。

- 加载脚本与节点创建：`load_driver.sh` 在 [driver/linux/tests/load_driver.sh:L42-L78](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tests/load_driver.sh#L42-L78) 按 `interrupt_selection` `case` 分支 `insmod ../xdma/xdma.ko`，并在 [driver/linux/tests/load_driver.sh:L88-L98](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tests/load_driver.sh#L88-L98) 用 `cat /proc/devices | grep xdma` 校验设备被识别。

- bypass/user BAR 节点的发现顺序：主机侧的 `reg_rw.py` 给出实际可用节点清单 [tool/hw/tests/lib/reg_rw.py:L17-L20](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/reg_rw.py#L17-L20)，按 `/dev/xdma0_user` 优先、`/dev/xdma0_bypass` 次之逐个试探，第一个能读通的就是接 `ctrl_lite` 的那个。

> 小贴士：`readme.txt` 提到「`/dev/xdma/card*` 便于区分多卡」。本讲后续一律按单卡、且 `ctrl_lite` 经 bypass/user BAR 访问来描述——这与 V10 平台 `npu_ctrl_lite.v` 注释里「Connected to: XDMA M_AXI_BYPASS port」一致。

#### 4.1.4 代码实践

**实践目标**：在 FPGA 主机上确认三类设备节点都存在，并把它们与三条通道对应起来。

**操作步骤**（需 FPGA 主机与已 `insmod` 的 `xdma.ko`，**待本地验证**）：

1. `ssh` 到 FPGA 主机，运行 `ls -l /dev/xdma0_*`，记录看到的节点名。
2. 对照预期：应至少有 `_h2c_0`、`_c2h_0`、`_bypass`（或 `_user`）。
3. 运行 `cat /proc/devices | grep xdma`，确认主设备号已注册。
4. 若节点缺失，`sudo ./driver/linux/tests/load_driver.sh`（在 FPGA 主机的镜像目录里）重新加载。

**需要观察的现象**：`/dev/xdma0_h2c_0`（写出口）、`/dev/xdma0_c2h_0`（读入口）、`/dev/xdma0_bypass` 或 `/dev/xdma0_user`（控制窗口）三者都在。

**预期结果**：三类节点齐全；缺任何一类都意味着驱动未加载或比特流未把对应 AXI 暴露给 BAR。

> 若你没有 FPGA 主机，可改为**源码阅读型实践**：在 [tool/hw/tests/lib/xdma.py:L17-L21](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py#L17-L21) 找到 `DEV_PREFIX = "/dev/xdma0"`，再在 `xdma.py` 的 `h2c`/`c2h` 方法里看到它们分别拼出 `_h2c_{channel}` 与 `_c2h_{channel}`，从而在无硬件的情况下把「节点名 ↔ 通道」对应关系读出来。

#### 4.1.5 小练习与答案

**练习 1**：为什么 kick NPU 用的是 `_bypass`/`_user` 设备，而不是 `_h2c_0`？

**参考答案**：`ctrl_lite` 是 AXI4-Lite 从设备，挂在 XDMA 的 bypass（AXI-Lite）窗口后面，只暴露 32 位控制/状态寄存器，需要单字 PIO 访问；而 `_h2c_0` 是走 128 位 AXI4 的 SGDMA 数据通道，用来搬大批数据，单字 PIO 既不顺手也不是它的用途。

**练习 2**：`load_driver.sh` 里 `interrupt_selection=4` 与默认分支有什么区别？

**参考答案**：`4` 对应 `insmod xdma.ko poll_mode=1`，即 DMA 完成不靠中断、靠轮询；默认分支则先用 `lspci` 探测板卡支持 MSI-X / MSI / Legacy，再选对应的 `interrupt_mode=` 参数插入。详见 [driver/linux/tests/load_driver.sh:L60-L78](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tests/load_driver.sh#L60-L78)。

---

### 4.2 reg_rw：寄存器级 mmap 读写

#### 4.2.1 概念说明

`reg_rw` 是 XDMA 工具包里最简单的程序，却最能体现「BAR = 一段可 `mmap` 的地址窗口」这一思想。它解决的问题是：主机想读写设备上的**某个 32 位寄存器**（例如 `ctrl_lite` 的控制字），怎么办？

答案是把 BAR 字符设备 `mmap` 进进程地址空间，然后对指针解引用——一次普通的内存读写，在内核里就被翻译成一次对 BAR 窗口的 PIO，再经 PCIe 落到设备寄存器。这比 `read`/`write` 系统调用轻得多，因为一次访存指令就完成一次寄存器事务。

`reg_rw` 的命令行约定是：`reg_rw <device> <address> [[type] data]`。给 3 个参数就是读，给 5 个参数就是写；`type` 取 `b`/`h`/`w` 表示字节/半字/字。

#### 4.2.2 核心流程

```
reg_rw /dev/xdma0_bypass 0x0 w            （读）
reg_rw /dev/xdma0_bypass 0x0 w 0x1        （写）

argv 解析 ──► open(device, O_RDWR|O_SYNC)
         ──► 计算 addr 在页内的偏移 offset 与页对齐基址 target_aligned
         ──► mmap(NULL, offset+4, RW, SHARED, fd, target_aligned)
         ──► map += offset
         ──► 读：*map 解引用（按宽度）；写：*map = value
         ──► munmap；close(fd)
```

要点：

- `O_SYNC`：把这次打开标成「同步、不走缓存」，保证 MMIO 直达设备而不是被 CPU 缓存吃掉。
- **页对齐**：`mmap` 的偏移参数必须页对齐，所以 `reg_rw` 把目标地址拆成「页基址 `target_aligned`」+「页内偏移 `offset`」，`mmap` 页基址，再把返回指针前移 `offset`。
- **宽度**：根据 `b`/`h`/`w` 用 `uint8_t*`/`uint16_t*`/`uint32_t*` 解引用，并在大端机上用 `ltohl`/`htoll` 做小端字节序转换。

#### 4.2.3 源码精读

- 用法与「参数个数决定读写」的约定：[driver/linux/tools/reg_rw.c:L51-L59](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L51-L59) 的 usage 字符串说明 `<device> <address> [[type] data]`，`type` 是 `[b]yte, [h]alfword, [w]ord`。

- 打开设备时强制同步、禁缓存：[driver/linux/tools/reg_rw.c:L88-L92](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L88-L92) `open(argv[1], O_RDWR | O_SYNC)`。

- 整个机制的核心——`mmap` + 前移页内偏移：[driver/linux/tools/reg_rw.c:L95-L105](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L95-L105)。`mmap(NULL, offset + 4, PROT_READ|PROT_WRITE, MAP_SHARED, fd, target_aligned)` 把 BAR 的那一页映射进来，随后 `map += offset` 对准目标地址。

- 读路径就是一次指针解引用（32 位字）：[driver/linux/tools/reg_rw.c:L123-L130](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L123-L130) `read_result = *((uint32_t *) map);`，并打印成 `0x%08x`。

- 写路径同理，向指针赋值：[driver/linux/tools/reg_rw.c:L155-L160](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L155-L160) `writeval = htoll(writeval); *((uint32_t *) map) = writeval;`。

- 主机侧如何调用 `reg_rw`、如何解析它的输出：[tool/hw/tests/lib/xdma.py:L51-L64](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py#L51-L64) 把命令拼成 `sudo reg_rw <device> <hex(offset)> w`（读）或 `... w <hex(value)>`（写），并用正则抓输出里**最后一个** `0x...` 作为寄存器值（注意不是第一个，第一个是地址）。

> 小贴士：`reg_rw` 与 4.3 的 `dma_*` 是两套完全不同的访问机制——`reg_rw` 是 `mmap` + 解引用的 PIO，`dma_*` 是 `write`/`read` + `lseek` 的 SGDMA。这是本讲最重要的一处分界，务必分清。

#### 4.2.4 代码实践

**实践目标**：用 `reg_rw` 读 `ctrl_lite` 控制字，并理解 `O_SYNC` 与页对齐的作用。

**操作步骤**（需 FPGA 主机，**待本地验证**）：

1. 读控制字：`sudo ./reg_rw /dev/xdma0_bypass 0x0 w`（或 `/dev/xdma0_user`，视哪条通）。
2. 观察输出行：应形如 `Read 32-bit value at address 0x0 (0x...): 0x00000000`（空闲态 `start/done/busy` 全 0）。
3. 把 `reg_rw.c` 里 `O_RDWR | O_SYNC` 临时改成 `O_RDWR`（仅用于理解，不要提交），重新编译再读一次，对比是否读到陈旧缓存值（取决于平台，不一定可复现，重在理解 `O_SYNC` 的意图）。

**需要观察的现象**：空闲态读到 `0x0`；若读到 `0xFFFFFFFF` 则是 PCIe completion-error 哨兵，说明 BAR 没映射好或 fabric 没活——这正是 [tool/hw/tests/test_bar_ctrl_lite.py:L19-L30](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/test_bar_ctrl_lite.py#L19-L30) 要断言「`0xFFFFFFFF` = 未映射/超时」的原因。

**预期结果**：读到 `0x00000000`（或 kick 后 `done`/`busy` 位置位的非零值）。

> 无硬件时改做**源码阅读型实践**：阅读 `reg_rw.c` 的 `main`，解释为什么读用 `argc <= 4` 判定（[reg_rw.c:L107](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L107)）、写用 `argc >= 5` 判定（[reg_rw.c:L140](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L140)）。答案：第 4 个参数是宽度 `type`，只有给到第 5 个参数 `data` 才构成一次写。

#### 4.2.5 小练习与答案

**练习 1**：`reg_rw` 为什么要算 `target_aligned = target & (~(pgsz - 1))` 再 `map += offset`？

**参考答案**：因为 `mmap` 要求文件偏移页对齐。目标地址 `target` 不一定页对齐，所以把它拆成「向下对齐的页基址」交给 `mmap`，再把映射到的虚拟地址前移「页内偏移 `offset = target & (pgsz-1)`」对准真正的寄存器。

**练习 2**：`xdma.py` 的 `reg_read` 解析输出时为什么要取**最后一个** `0x...` 而不是第一个？

**参考答案**：`reg_rw` 的输出形如 `... address 0x0 (0x7f...): 0xDEADBEEF`，第一个 `0x...` 是地址、括号里是映射基址、最后一个才是读出的寄存器值。取错位置会拿到地址而非数据。见 [tool/hw/tests/lib/xdma.py:L58-L64](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py#L58-L64) 的注释。

---

### 4.3 dma_to_device / dma_from_device：SGDMA 数据搬运

#### 4.3.1 概念说明

`reg_rw` 一次只能动一个字，把 32 字节的矩阵 A 或 128 字节的累加器搬来搬去不能靠它。批量数据搬运用 `dma_to_device`（H2C）与 `dma_from_device`（C2H），它们基于内核的 SGDMA：主机把一段缓冲交给驱动，驱动提交散列-聚集传输，由 DMA 引擎把数据在主机内存与板卡 DDR3 之间搬移。

这里有一个**极其关键、却容易看漏**的设计：DMA 工具把「AXI 总线地址」当成**文件偏移**来用。也就是说，你先 `lseek(fd, axi_addr, SEEK_SET)`，再 `write(fd, buf, size)`——内核驱动把这个文件位置 `pos` 当作 DMA 的目的地址，发起一次到 `axi_addr` 的传输。这正是 u8-l3 PythonDriver 文档里那句「DMA 用 `pwrite`/`pread`，文件偏移即 AXI 地址」的来源，也是 vendor `cdev_sgdma.c` 把 `*pos` 传进 `xdma_xfer_submit` 的语义。

#### 4.3.2 核心流程

`dma_to_device` 的数据流（写 DDR3）：

```
getopt: -d 设备 -a AXI地址 -s 字节 -f 输入文件 [-c 次数]
   ──► open(设备) ; 若有 -f 则把文件 read 进 buffer（posix_memalign 对齐）
   ──► for i in 0..count-1:
           write_from_buffer(dev, fd, buffer, size, addr)   # addr = AXI 目的地址
              └─ lseek(fd, addr, SEEK_SET)
              └─ write(fd, buf, size)          # 内核把它翻译成一次 SGDMA
   ──► 统计带宽（用 clock_gettime 测耗时）
```

`dma_from_device` 是对偶：`-d /dev/xdma0_c2h_0 -a AXI地址 -s 字节 -w 输出文件`，用 `read()` 把 DDR3 的数据读进 buffer，再落盘。

可选项 `-k aperture`：当 AXI 地址高位是「孔径窗口选择」时，走 `ioctl(IOCTL_XDMA_APERTURE_W)` 而非 `write`，把 `ep_addr`/`aperture` 一起传给驱动。本讲的 NPU staging 地址不在此列，直接用 `lseek`+`write` 即可。

#### 4.3.3 源码精读

- 命令行选项表与默认值：[driver/linux/tools/dma_to_device.c:L31-L47](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L31-L47) 定义 `device/address/aperture/size/offset/count/infile/outfile` 等长选项与默认值。

- `-a` 把字符串解析成 64 位 AXI 地址：[driver/linux/tools/dma_to_device.c:L125-L128](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L125-L128) `address = getopt_integer(optarg)`（`getopt_integer` 支持 `0x` 前缀，见 [dma_utils.c:L30-L41](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_utils.c#L30-L41)）。

- 打开设备：[driver/linux/tools/dma_to_device.c:L186](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L186) `fpga_fd = open(devname, O_RDWR)`。

- **关键调用**——把 `addr`（AXI 目的地址）作为 `base` 传进 `write_from_buffer`：[driver/linux/tools/dma_to_device.c:L263-L265](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L263-L265)，`write_from_buffer(devname, fpga_fd, buffer, size, addr)`。

- `write_from_buffer` 的核心：把 `base`（即 AXI 地址）`lseek` 成文件偏移，再 `write`：[driver/linux/tools/dma_utils.c:L95-L111](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_utils.c#L95-L111)，签名 `write_from_buffer(fname, fd, buffer, size, base)`，内部 `lseek(fd, offset, SEEK_SET)` 把 `offset = base` 设为文件位置。这一行是「文件偏移 = AXI 地址」语义的落点。

- 孔径路径（非本讲主路径，了解即可）：[driver/linux/tools/dma_to_device.c:L244-L261](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L244-L261)，当传了 `-k aperture` 时走 `ioctl(IOCTL_XDMA_APERTURE_W, &io)`，把 `ep_addr` 与 `aperture` 一起下发。

- 主机侧真实命令行：[tool/hw/tests/lib/xdma.py:L87-L95](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py#L87-L95) 把 H2C 搬运拼成 `sudo dma_to_device -d /dev/xdma0_h2c_0 -f <tmp> -s <size> -a <hex(ddr_offset)>`；C2H 在 [xdma.py:L110-L116](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py#L110-L116) 用 `dma_from_device -d /dev/xdma0_c2h_0 ...`。

> 小贴士：缓冲对齐很重要——[dma_to_device.c:L223](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L223) 用 `posix_memalign(&allocated, 4096, size + 4096)` 按 4 KiB 对齐，这是 DMA 引擎对主机物理页连续性的常见要求；u8-l3 的 pybind11 边界也会强制对齐。

#### 4.3.4 代码实践

**实践目标**：用 `dma_to_device` 把 32 字节写进 DDR3，再用 `dma_from_device` 读回来比对，体会「文件偏移 = AXI 地址」。

**操作步骤**（需 FPGA 主机，**待本地验证**）：

1. 准备 32 字节文件 `a.bin`（例如 32 个 `0xAB`）。
2. 写到 DDR3 偏移 `0x0_4000_0000`（这正是下文 4.4 要讲的矩阵 A 的 staging 地址）：
   `sudo ./dma_to_device -d /dev/xdma0_h2c_0 -f a.bin -s 32 -a 0x40000000`。
3. 读回 32 字节：`sudo ./dma_from_device -d /dev/xdma0_c2h_0 -w out.bin -s 32 -a 0x40000000`。
4. `cmp a.bin out.bin` 比对。

**需要观察的现象**：`dma_to_device` 打印 `** Average BW = ...`；`cmp` 无差异表示 DDR3 往返完整。

**预期结果**：`cmp` 静默退出（一致）。这一步等价于 [xdma.py:L127-L144](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py#L127-L144) 的 `loopback_check`。

> 无硬件时改做**源码阅读型实践**：在 `dma_utils.c` 里比较 `read_to_buffer`（[dma_utils.c:L43-L93](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_utils.c#L43-L93)）与 `write_from_buffer`（[dma_utils.c:L95-L146](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_utils.c#L95-L146)），指出二者都把 `base` 作为 `offset` 传给 `lseek`，从而把「文件偏移 = AXI 地址」的语义对称地用于读写两个方向。

#### 4.3.5 小练习与答案

**练习 1**：为什么不传 `-a` 时数据会写到 DDR3 的地址 `0x0`？

**参考答案**：`address` 默认为 `0`（[dma_to_device.c:L105](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/dma_to_device.c#L105)），它作为 `base` 传给 `write_from_buffer`，进而 `lseek(fd, 0)`，于是 SGDMA 把数据搬到 AXI 地址 0。

**练习 2**：`dma_to_device` 与 `reg_rw` 在「如何把地址告诉驱动」上有何本质区别？

**参考答案**：`reg_rw` 把地址作为 `mmap` 的偏移参数，映射后用指针解引用；`dma_to_device` 把地址作为文件偏移用 `lseek` 设置，再用 `write`。前者是 MMIO（一次访存指令），后者是 SGDMA（内核提交传输）。两者都依赖「位置即地址」，但一个走 BAR 窗口的 PIO，一个走 DMA 通道的突发传输。

---

### 4.4 ctrl_lite 协议与 C 工具驱动的完整 MMALU 序列

#### 4.4.1 概念说明

前面三个模块分别讲了「节点」「寄存器访问」「数据搬运」。现在把它们接上 NPU。

`ctrl_lite` 是 NPU 暴露给主机的**唯一控制接口**——一个 AXI4-Lite 从设备，只占一个 32 位寄存器，三比特协议：

| 比特 | 名字 | 方向 | 含义 |
|:-----|:-----|:-----|:-----|
| `[0]` | `start` | 主机写 | 写 1 产生 1 拍 `start` 脉冲，自清，用来 kick `npu_dma_master` |
| `[1]` | `done` | 主机读 | `npu_dma_master` 完成一轮（结果已写回 DDR3）时拉高 1 拍；`ctrl_lite` 把它锁存到下一次 `start` |
| `[2]` | `busy` | 主机读 | `npu_dma_master` FSM 活动期间为高电平 |

kick 之后，**所有真正的工作由 `npu_dma_master` 自主完成**：它从 DDR3 的固定 staging 地址读 A/B/ACCUM，喂给 MMALU，等 `io_clct`，再把 OUT 写回 DDR3。主机只负责「事先把 A/B/ACCUM 写到那几个地址」+「kick」+「轮询 `done`」+「从 OUT 地址读结果」。这就是 u8-l1 所说的 `ctrl_lite + DMA staging` 协议。

staging 地址表（K=32、N=8）由 `npu_dma_master` 的参数钉死，落在统一地址空间 MIG C0 区、+1 GB 处，远离主机常见的近 0x0 暂存区：

| 操作数 | DDR3 地址 | 大小 | 内容 |
|:-------|:----------|:-----|:-----|
| A      | `0x0_4000_0000` | 32 B | 32 × int8 |
| B      | `0x0_4000_0100` | 32 B | 32 × int8 |
| ACCUM  | `0x0_4000_0200` | 128 B | 32 × int32 |
| OUT    | `0x0_4000_0400` | 128 B | 32 × int32 |

> 关于「诚实边界」：仓库里**没有**一个把上述步骤串成一气的 C 程序——vendor 工具是通用的，NPU 特有的 staging 地址表与 `ctrl_lite` 寄存器映射都活在 RTL（`npu_dma_master.v` 与 `npu_ctrl_lite.v`）里。主机侧的编排，要么由 u8-l3 的 Python 驱动（pybind11 边界内做），要么由 `tool/hw/tests` 的 SSH 测试框架（拼 `reg_rw`/`dma_*` 命令行）做。本讲把这条序列**从 RTL 契约重建**出来，并指出它在主机侧的精确 C 工具实现。

#### 4.4.2 核心流程

**硬件侧 `npu_dma_master` 的状态机**（kick 之后自主跑）：

```
S_IDLE ──start──► S_READ_A_AR ──► S_READ_A_R   读 A (2 beats, 128-bit)
                                  ──► S_READ_B_AR ──► S_READ_B_R   读 B
                                  ──► S_READ_ACC_AR ──► S_READ_ACC_R 读 ACCUM (8 beats)
                                  ──► S_KICK        把 a_buf/b_buf/acc_buf 驱到 MMALU 端口
                                                     io_ctrl_use_accum=1, io_ctrl_busy=1
                                  ──► S_WAIT_CLCT   等 io_clct，锁存 io_out_* 到 out_buf
                                  ──► S_WR_AW/S_WR_W/S_WR_B  把 out_buf 写回 OUT 地址 (8 beats)
                                  ──► S_DONE        done<=1, busy<=0
```

**主机侧 C 工具序列**（与上面严格对应）：

```
# 1) staging：把 A/B/ACCUM 写到它们各自的 DDR3 地址
dma_to_device -d /dev/xdma0_h2c_0 -f a.bin     -s 32  -a 0x40000000
dma_to_device -d /dev/xdma0_h2c_0 -f b.bin     -s 32  -a 0x40000100
dma_to_device -d /dev/xdma0_h2c_0 -f accum.bin -s 128 -a 0x40000200

# 2) kick：向 ctrl_lite 写 start=1（经 bypass BAR）
reg_rw /dev/xdma0_bypass 0x0 w 0x1

# 3) wait：轮询 done（bit1）
while ((reg_rw /dev/xdma0_bypass 0x0 w) & 0x2) == 0: sleep

# 4) read OUT：从 OUT 地址读回 128 字节
dma_from_device -d /dev/xdma0_c2h_0 -w out.bin -s 128 -a 0x40000400
```

`ctrl_lite` 控制字在 bypass BAR 中的访问方式：offset `0x0`（`REG_CTRL`），32 位字，经 `reg_rw /dev/xdma0_bypass 0x0 w` 读、`reg_rw /dev/xdma0_bypass 0x0 w 0x1` 写。

#### 4.4.3 源码精读

**`ctrl_lite` 寄存器映射与三比特协议**（硬件侧）：

- 寄存器表注释：[ip/vivado/xc7k480t/src/npu_ctrl_lite.v:L4-L9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v#L4-L9)，写明 `[0]start W`、`[1]done RO`、`[2]busy RO`，且注释指出它「Connected to: XDMA M_AXI_BYPASS port」——这正是 4.1 里「`ctrl_lite` 经 bypass BAR 访问」的依据。

- 写 `start` 脉冲：主机写 `wdata[0]=1` 时，[npu_ctrl_lite.v:L117-L123](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v#L117-L123) 置 `ctrl_start_r<=1; start<=1`，`start` 默认值在 [L101](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v#L101) 每拍清 0，所以是自清的 1 拍脉冲——对应「写 1 = kick」。

- 读路径回读 `busy`/`done`：[npu_ctrl_lite.v:L187-L194](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v#L187-L194) 把 `rdata` 拼成 `{..., busy[2], done_latch[1], 1'b0[0]}`，`start` 是 W-only、读出恒 0；`done_latch` 在 [L68-L77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v#L68-L77) 锁存 `done` 脉冲、并在下一次 `start` 清零，所以主机轮询 `done` 是稳态可读的。

**staging 地址表**（硬件侧）：

- `npu_dma_master` 顶部地址映射注释：[ip/vivado/xc7k480t/src/npu_dma_master.v:L27-L40](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L27-L40)，明确「A @ 0x0_4000_0000、B @ 0x0_4000_0100、ACCUM @ 0x0_4000_0200、OUT @ 0x0_4000_0400」，并说明「+1 GB 远离主机近 0x0 的暂存区」。参数定义在 [L37-L40](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L37-L40)（`DEFAULT_BASE_A/B/ACCUM/OUT`）。

- FSM 状态列表：[npu_dma_master.v:L228-L241](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L228-L241) 列出 `S_IDLE → S_READ_A_AR → S_READ_A_R → … → S_KICK → S_WAIT_CLCT → S_WR_AW → S_WR_W → S_WR_B → S_DONE`，与 4.4.2 流程图一一对应。

- `S_IDLE` 收到 `start`：[npu_dma_master.v:L288-L295](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L288-L295)，`if (start) begin busy<=1; state<=S_READ_A_AR; end`——这就是 `ctrl_lite` 的 `start` 脉冲如何启动整轮搬运。

- `S_KICK` 把 staging buffer 驱到 MMALU 端口：[npu_dma_master.v:L495-L498](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L495-L498)，`io_ctrl_use_accum<=1; io_ctrl_keep<=0; io_ctrl_busy<=1; state<=S_WAIT_CLCT`。

- `S_WAIT_CLCT` 等 `io_clct` 并锁存 `io_out_*`：[npu_dma_master.v:L504-L528](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L504-L528)，`if (io_clct)` 把 32 个 `io_out_*` 收进 `out_buf`，然后进 `S_WR_AW`。

- `S_WR_AW` 把 `out_buf` 写回 `DEFAULT_BASE_OUT`：[npu_dma_master.v:L552-L555](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L552-L555)，`m_axi_awaddr<=DEFAULT_BASE_OUT; m_axi_awlen<=ARLEN_FULL`（8 beats，128 字节）。这正是主机随后用 `dma_from_device` 从 `0x0_4000_0400` 读回的那 128 字节。

**主机侧如何把三比特协议映射成 `reg_rw`**：

- `ctrl_lite` 位定义：[drivers/chisel_npu_py/src/chisel_npu_py/consts.py:L18-L20](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/drivers/chisel_npu_py/src/chisel_npu_py/consts.py#L18-L20) 写明 `CTRL_START_BIT=0`、`CTRL_DONE_BIT=1`、`CTRL_BUSY_BIT=2`，与 RTL 注释完全一致。

- kick = 写 `0x1` 到 offset 0：[tool/hw/tests/lib/reg_rw.py:L77-L79](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/reg_rw.py#L77-L79) `kick_start` 调 `self.write(REG_CTRL, 0x1)`，而 `write` 最终发出 `reg_rw <dev> 0x0 w 0x1`（[xdma.py:L66-L69](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py#L66-L69)）。

- wait = 轮询 `done` 位：[tool/hw/tests/lib/reg_rw.py:L81-L89](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/reg_rw.py#L81-L89) `wait_done` 循环读 `REG_CTRL`，判 `(>>1)&1`（即 `done`），超时 10 秒；`is_busy`/`is_done` 在 [L69-L75](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/reg_rw.py#L69-L75)。

> 小贴士：注意 staging 地址表与操作数大小（A/B 各 32 B、ACCUM/OUT 各 128 B）的一致性——`npu_dma_master.v` 注释（[L33-L36](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L33-L36)）与 PythonDriver.md 的 staging 表（[docs/implementations/PythonDriver.md:L86-L93](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/docs/implementations/PythonDriver.md#L86-L93)）是同一张表的两面。主机 `dma_to_device` 的 `-s` 必须与硬件读取的 beat 数对应：A/B 是 2 个 128 位 beat（32 B）、ACCUM 是 8 个 beat（128 B）。

#### 4.4.4 代码实践

**实践目标**：阅读 `driver/linux/README.md` 与 `reg_rw.c`，把一次完整 MMALU 计算还原成 C 工具命令序列，并指出 `ctrl_lite` 控制字在 bypass BAR 中的访问方式。这是本讲的核心代码实践。

**操作步骤**（**源码阅读型 + 可选上板**）：

1. 读 [driver/linux/README.md:L13-L48](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/README.md#L13-L48)，理解 `xdma/`、`tools/`、`tests/` 的分工，以及「编辑 → rsync 到 FPGA 机 → 远程 make → `tool/hw/reboot_and_load.sh` 重载」的部署工作流。
2. 读 [reg_rw.c](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c) 的 `main`，确认「3 参数读、5 参数写」的判定（[L107](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L107) 与 [L140](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/driver/linux/tools/reg_rw.c#L140)）。
3. 对照 [npu_dma_master.v:L33-L40](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L33-L40) 的 staging 地址表，写出主机侧 4 条命令（3 条 `dma_to_device` staging + 1 条 `dma_from_device` 回读），并标注每条的 `-a` 地址与 `-s` 字节数。
4. 写出 kick 命令 `reg_rw /dev/xdma0_bypass 0x0 w 0x1`，并解释为何「写 `0x1`」就够、不需要写 `0x5`（同时置 `done`）——因为 `done` 是 RO，硬件忽略对它的写（见 [npu_ctrl_lite.v:L4-L9](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v#L4-L9)）。
5. （可选，需 FPGA，**待本地验证**）把上述命令在 FPGA 主机依次执行，用 `reg_rw` 轮询 `done`，再 `dma_from_device` 读 OUT，人工核对 `OUT[i] = A[i]·B[K-1] + ACCUM[i]`（这正是 u8-l3 里 `test_mmalu_compute.py` 的解析式校验）。

**需要观察的现象**：kick 后立即读 `ctrl_lite` 应看到 `busy`（bit2）置位；一段时间后 `done`（bit1）置位、`busy` 清零；此时 OUT 地址处的 128 字节即为结果。

**预期结果**：OUT 的 32 个 int32 满足上述解析式；若读到 `0xFFFFFFFF` 则是 BAR/fabric 没起来。

> 关键结论（本实践的答案骨架）：一次完整 MMALU 计算在 C 工具层面是「3 条 `dma_to_device` 把 A/B/ACCUM 暂存到 `0x4000_0000/0100/0200` → 1 条 `reg_rw … 0x0 w 0x1` kick → 反复 `reg_rw … 0x0 w` 轮询 bit1 → 1 条 `dma_from_device` 从 `0x4000_0400` 读 128 字节 OUT」。`ctrl_lite` 控制字经 **bypass BAR 的 offset `0x0`、32 位字** 访问，读用 `reg_rw <dev> 0x0 w`、写用 `reg_rw <dev> 0x0 w <值>`。

#### 4.4.5 小练习与答案

**练习 1**：为什么主机轮询 `done` 而不是 `busy` 的下降沿？

**参考答案**：`busy` 是电平、活动期间为高、结束就回低；但主机若在 kick 之后才第一次读，可能恰好错过它的下降沿。`done` 在 `ctrl_lite` 里被**锁存**（`done_latch`，[npu_ctrl_lite.v:L68-L77](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_ctrl_lite.v#L68-L77)），一旦完成就稳定保持到下一次 `start`，所以轮询 `done` 是稳健的「完成」判据。`reg_rw.py` 的 `wait_done` 正是判 `(ctrl_read() >> 1) & 1`。

**练习 2**：若主机把 A 写到 `0x4000_0000` 但忘了写 ACCUM（该地址仍是旧值），MMALU 会怎样？

**参考答案**：`npu_dma_master` 会照常从 `0x4000_0200` 读 128 字节作为 ACCUM（可能是上轮残留或 DDR 上电默认值），并在 `S_KICK` 置 `io_ctrl_use_accum=1` 把它加进结果。staging buffer 虽在 reset 时清零（[npu_dma_master.v:L146-L153](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L146-L153)），但 DDR3 不会自动清——所以主机必须显式写 ACCUM，否则用的是陈旧数据。

**练习 3**：staging 地址为何放在 `+1 GB`（`0x4000_0000`）而不是 `0x0`？

**参考答案**：见 [npu_dma_master.v:L30-L32](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v#L30-L32) 注释——主机 PCIe DMA 的暂存区通常在近 `0x0`，把 NPU 操作数放到 `+1 GB` 可避免与主机暂存冲突。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「纸上驱动 NPU」：

1. **画出软件栈分层图**：从上到下标出「你的程序 / `dma_*` 与 `reg_rw`（用户态 C 工具）/ `/dev/xdma0_*`（字符设备）/ `xdma.ko`（内核驱动）/ XDMA IP + AXI fabric + DDR3 + `npu_subsys`」，并在 `npu_subsys` 里再画出 `ctrl_lite ↔ npu_dma_master ↔ MMALU` 三者。
2. **重建命令序列**：给定输入 `A[i]=i`（int8，i=0..31）、`B[i]=1`（int8）、`ACCUM[i]=0`（int32），写出：
   - 三条 `dma_to_device` staging 命令（标出 `-a` 与 `-s`）；
   - kick 与轮询的 `reg_rw` 命令；
   - 回读 OUT 的 `dma_from_device` 命令。
3. **手算期望值**：按 `OUT[i] = A[i]·B[K-1] + ACCUM[i]`（`K=32`，`B[K-1]=B[31]=1`），算出 `OUT[0..31]`，写成一个 32 元 int32 数组。
4. **定位访问方式**：在图上标出「`ctrl_lite` 控制字 = bypass BAR 的 offset `0x0`、32 位字」「A/B/ACCUM/OUT 各自的 DDR3 地址」。
5. （可选，**待本地验证**）在 FPGA 主机执行序列，把读回的 OUT 与第 3 步的手算值逐 lane 比对。

> 这个综合实践刻意复刻了 u8-l3 Python 驱动里 `test_mmalu_compute.py` 的「stage → kick → wait → read → 解析式校验」五步，只不过你用的是裸 C 工具命令行而非 numpy API。做完后你会很自然地理解：u8-l3 的 pybind11 边界为什么要把「DDR 地址、寄存器偏移、传输」全部收进 C++——因为这些正是本讲里最容易写错（地址/大小/位段）的部分。

## 6. 本讲小结

- `xdma.ko` 加载后在 `/dev/xdma0_*` 下创建三类节点：`_h2c_*`（写出口）、`_c2h_*`（读入口）、`_bypass`/`_user`（控制 BAR 窗口），分别对应高速 SGDMA 数据通路与轻量 AXI-Lite 控制通路。
- 两条访问路径本质不同：`reg_rw` 走 `mmap` + 指针解引用的 PIO 单字读写；`dma_to_device`/`dma_from_device` 走 `write`/`read` + `lseek` 的 SGDMA 批量搬运，且**文件偏移即 AXI 地址**。
- `ctrl_lite` 是 NPU 唯一控制接口：一个 32 位寄存器、三比特协议（`start` 写、`done`/`busy` 读），经 bypass BAR 的 offset `0x0` 用 `reg_rw` 访问；`done` 被锁存到下一次 `start`，故轮询 `done` 稳健。
- kick 之后 `npu_dma_master` 自主完成「读 A/B/ACCUM → 喂 MMALU → 等 `io_clct` → 写 OUT」，staging 地址表固定在 `0x4000_0000/0100/0200/0400`（A/B/ACCUM/OUT）。
- 一次完整 MMALU 计算的 C 工具序列是：3 条 `dma_to_device` 暂存 → 1 条 `reg_rw` kick → 轮询 `reg_rw` 的 `done` 位 → 1 条 `dma_from_device` 回读 OUT。
- vendor C 工具是通用的，NPU 特有的契约（`ctrl_lite` 寄存器映射、staging 地址表）活在 RTL；仓库里没有把它们串成一气的 C 程序，主机侧编排由 u8-l3 的 Python 驱动或 `tool/hw/tests` 的 SSH 框架承担。

## 7. 下一步学习建议

- 下一讲 **u8-l3（Python 用户态驱动 chisel_npu_py）** 会把本讲的 `reg_rw`/`dma_*` 命令序列收进一个 pybind11 C++ 边界，对外只暴露「numpy 优先、按操作数名寻址、地址不可见」的 API（`ChiselNPU.mmalu(A, B, ACCUM)`）。学完它你会看到：本讲里你需要手动算的 staging 地址、字节大小、`done` 位偏移，在 u8-l3 里全部由 C++ 模块独占，Python 侧一个都看不到——这正是 `docs/implementations/PythonDriver.md` 所说的「严格 pybind11 边界」。
- 想加深对 staging 地址表与 FSM 的理解，可直接精读 [ip/vivado/xc7k480t/src/npu_dma_master.v](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/ip/vivado/xc7k480t/src/npu_dma_master.v)，特别留意它如何处理 AXI 读超时重发（`READ_TIMEOUT_MAX`）与 OUT 写回的 off-by-one 修复注释。
- 想看主机侧如何**真的**把这三条工具拼起来跑，可读 [tool/hw/tests/lib/xdma.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/xdma.py) 的 `h2c`/`c2h`/`reg_read`/`reg_write`，以及 [tool/hw/tests/lib/reg_rw.py](https://github.com/mpskex/chisel-npu/blob/3e0d1314e9572c17fb40f206f0d1e7a72a80b663/tool/hw/tests/lib/reg_rw.py) 的 `kick_start`/`wait_done`——它们是本讲命令序列的最权威实现。
- 若对 XDMA 驱动本身的内核态实现（`cdev_sgdma.c` 如何把 `*pos` 传进 `xdma_xfer_submit`）感兴趣，可进入 `driver/linux/xdma/` 阅读 kernel 模块源码，但那已超出本讲与本项目主线。
