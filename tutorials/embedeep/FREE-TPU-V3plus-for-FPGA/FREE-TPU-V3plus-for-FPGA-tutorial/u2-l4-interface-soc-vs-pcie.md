# 接口选择：SoC 内存映射 vs PCIE/XDMA

## 1. 本讲目标

u2-l3 已经带你在 Linux 路线下跑通了一次分类推理，并且提到初始化要配齐「三件套」：**接口类型、寄存器 zone、数据基地址**。本讲把其中最关键的一件——**接口类型**——单独拧出来讲透。读完本讲，你应该能够：

- 说清 `eepInterfaceType_SOC` 与 `eepInterfaceType_PCIE` 这两种接入方式的**物理含义**：前者是 TPU 和 ARM 核在同一颗 ZynqMP 芯片里、靠 AXI 内存映射访问；后者是 TPU 做成 PCIe 卡插在主机上、靠 Xilinx XDMA 桥接访问。
- 看懂 `EEPTPU_REG_ZONE {core_id, addr, size}` 在两种接口下分别填什么值，并能解释 **arm64 的 `0xA0000000` 与 arm32 的 `0x43C00000` 为什么不同**。
- 说出 PCIE 模式下那三个 `/dev/xdma0_*` 设备文件各自干什么用。
- 区分 classify demo 的**运行时切换**与 yolo demo 的**编译时宏 `INTERFACE_PCIE` 切换**，并知道怎么把一个 demo 从 SoC 模式改成 PCIE 模式。

本讲只讲 Linux 路线（`libeeptpu_pub` + demo）。裸机 standalone 下的寄存器协议在 U4 单元讲，本讲不展开。

## 2. 前置知识

进入源码前，先承接三个前置结论（都来自前面的讲义，这里只做最短的复述）：

1. **两条 AXI 通路**（来自 u1-l3 / u1-l4）：ZynqMP 上 TPU 有两条到 PS 的通路——**控制通路**（PS 写 TPU 的控制寄存器，物理地址落在 `0xA0000000`）和**数据通路**（TPU 经 HP 口读写 DDR 里的张量）。这两个地址由 Vivado 工程的 `assign_bd_address` 定死，软件只能照抄。本讲你会再次看到 `0xA0000000`。
2. **EEPTPU 类的调用顺序**（来自 u2-l3）：`init → set_base_address → set_interface → load_bin → set_input → forward → 读结果 → close`。本讲聚焦最前面的「配地址 + 选接口」这一段。
3. **库头文件不在仓库里**（来自 u2-l3）：`EEPTPU` 类、`EEPTPU_REG_ZONE`、接口类型枚举等声明都在 `eeptpu.h` 里，而 `eeptpu.h` 随**闭源**的 `libeeptpu_pub` 分发（编译时由 `../libs/${pf}/eep/include` 提供，不在本仓库内）。因此本讲**不臆造头文件里的字段定义**，所有 API 描述都严格依据 `main.cpp` 里对它们的**真实调用**——这些调用本身就是最可靠的接口说明书。

还有一个术语要先讲清楚：**内存映射（memory-mapped）**。CPU 访问外设寄存器有两种经典方式——`port I/O`（用专门的指令端口）和**内存映射 I/O**（把外设寄存器「铺」在一段物理地址上，CPU 用普通的 `load/store` 指令读写那块地址就等于读写寄存器）。ARM/Zynq 体系几乎都用后者。所以「SoC 内存映射接口」说白了就是：**TPU 的寄存器和 DDR 一样出现在 CPU 的物理地址空间里，CPU 直接读写地址即可**。

## 3. 本讲源码地图

本讲只涉及两个 demo 的主文件，以及一个编译脚本：

| 文件 | 作用 | 是否在仓库内 |
| --- | --- | --- |
| `sdk/demo/classify/main.cpp` | 分类 demo：**运行时**切换 SoC/PCIE，含 arm64 与 arm32 两套 SoC 地址 | ✅ 可读源码 |
| `sdk/demo/yolo/main.cpp` | 检测 demo：用**编译时宏** `INTERFACE_PCIE` 切换接口 | ✅ 可读源码 |
| `sdk/demo/classify/compile.sh` | 交叉编译脚本：按平台切换编译器，本讲用来演示「如何注入 `-DINTERFACE_PCIE`」 | ✅ 可读源码 |
| `eeptpu.h`（来自 `libeeptpu_pub`） | 接口类型枚举、`EEPTPU_REG_ZONE`、`EEPTPU` 类声明 | ❌ 闭源库附带 |

> 补充：除了 classify / yolo，`sdk/demo/multi_bins_test/main.cpp` 也采用了和 yolo 一样的 `#if defined(INTERFACE_PCIE)` 编译时切换。本讲只精读 classify 与 yolo，但结论对 multi_bins_test 同样成立（多核实例化会在 u7-l1 讲）。

## 4. 核心概念与源码讲解

### 4.1 接口类型枚举：两种「TPU 在哪、怎么够得着」

#### 4.1.1 概念说明

`libeeptpu_pub` 把「软件如何物理地够到 TPU」抽象成一个枚举（定义在闭源的 `eeptpu.h` 里），在 `main.cpp` 里以两个常量出现：

- `eepInterfaceType_SOC`：**片上系统（System-On-Chip）内存映射**。TPU IP 烧在 ZynqMP 的 FPGA 可编程逻辑（PL）里，和 ARM 处理器（PS）封在同一颗芯片/同一个封装内。PS 通过 AXI 总线把 TPU 的寄存器和数据「映射」进自己的物理地址空间，于是 CPU 用一条普通的内存读写指令就能动 TPU 的寄存器。这是本仓库**默认**也是最常见的形态（你拿到的 `BOOT.BIN` 就是这种板卡）。

- `eepInterfaceType_PCIE`：**PCIe 加速卡 + XDMA**。TPU 做成一张 PCIe 板卡插在主机（x86 或 ARM）的 PCIe 插槽上。主机 CPU **不能**直接寻址到卡上的 AXI 地址空间，于是卡端用一颗 Xilinx 的 **XDMA** IP 把「PCIe 事务」翻译成「AXI 事务」。主机侧加载 Xilinx 开源的 xdma 内核驱动后，会看到一组字符设备文件，软件通过读写这些文件来完成寄存器读写和 DMA 搬数据。

一句话对比：**SoC 是「TPU 在我家隔壁，走门牌号（物理地址）就能找到」；PCIE 是「TPU 在河对岸，得靠 xdma 这座桥（字符设备）传话」。**

#### 4.1.2 核心流程

无论哪种接口，初始化的骨架是一样的，区别只在「填什么参数」：

```
1. tpu = tpu->init()                           # 工厂入口，拿到真正的 EEPTPU 对象
2. 【按接口类型填配置】                            ← 本讲的重点
     PCIE：  set_interface_info_pcie(三个 xdma 设备)
             set_tpu_mem_base_addr(0)
             注册 reg_zone{core_id=0, addr=0x00040000, size=256KB}
             set_base_address(0,0,0,0)
     SoC：   注册 reg_zone{core_id=0, addr=<物理地址>, size=0x1000}
             set_base_address(<DDR 基地址>,...,共 4 个)
3. tpu->eeptpu_set_interface(interface_type)   # 最终把后端切到 SOC 或 PCIE 实现
4. tpu->eeptpu_load_bin(path_bin)              # 加载网络
```

第 3 步的 `eeptpu_set_interface(interface_type)` 是真正的「切换开关」：库内部会根据传入的枚举值，把后续 `set_input`/`forward` 时用到的底层读写函数指向 **SOC 实现**（走 `/dev/mem` mmap）或 **PCIE 实现**（走 xdma 字符设备）。

#### 4.1.3 源码精读

classify demo 在文件顶部用一个普通的全局变量保存接口类型，默认 SoC：

[sdk/demo/classify/main.cpp:17-18](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L17-L18) — 声明 `eep_interface_type`，默认值为 `eepInterfaceType_SOC`，下一行被注释掉的 `eepInterfaceType_PCIE` 是「想用 PCIE 时取消注释」的提示。这是**运行时**切换的入口。

`eeptpu_init` 随后用 `if/else if` 在运行时分支：

[sdk/demo/classify/main.cpp:35-51](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L35-L51) — 先判 `eepInterfaceType_PCIE`，再判 `eepInterfaceType_SOC`。两段分支体里填的参数截然不同（后面两节细讲）。

最后统一收口：

[sdk/demo/classify/main.cpp:74-75](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L74-L75) — 不论走了哪个分支，最终都调用 `tpu->eeptpu_set_interface(interface_type)`，把后端实现切到对应接口。

#### 4.1.4 代码实践

**实践目标**：在不改任何代码的前提下，确认 classify demo 当前默认走哪种接口、并定位「切换点」。

**操作步骤**：

1. 打开 `sdk/demo/classify/main.cpp`，找到第 17 行。
2. 确认 `eep_interface_type` 的当前取值是 `eepInterfaceType_SOC`（第 18 行的 PCIE 行被注释）。
3. 顺着 `eeptpu_init` 看第 35 行的 `if` 与第 51 行的 `else if`，确认运行时只会走其中一条。
4. 找到第 74 行 `eeptpu_set_interface`，理解它是「真正的开关」。

**需要观察的现象**：默认配置下，程序运行时会在串口/终端打印初始化信息（库版本、硬件版本等），但**不会**打印 `"Interface type: PCIE \n"`——因为这句 `printf` 只在 PCIE 分支里（第 37 行）。

**预期结果**：默认运行 SoC 模式，看不到 `Interface type: PCIE` 这行；若把第 17、18 行互换（启用 PCIE），运行时就会先打印 `Interface type: PCIE`。

> 本实践为**源码阅读型**，无需硬件即可完成阅读部分；实际运行需在对应板卡/PCIe 卡上验证（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 classify demo 不需要重新编译就能在 SoC 和 PCIE 两种硬件之间切换？

> **答案**：因为 classify 用的是**运行时变量** `eep_interface_type`（第 17 行），两条分支的代码都被编译进了同一个可执行文件，切换只需要改这个变量的取值（即注释/取消注释第 17、18 行）再重跑，不必换编译器或加宏。

**练习 2**：`eeptpu_set_interface(interface_type)` 这一句如果删掉，会发生什么？

> **答案**：前面 `set_base_address`/`set_tpu_reg_zones` 等只是在「填表」，库还没把后端读写函数指过去；少了 `set_interface` 这一步，库不知道该用 SOC 还是 PCIE 的底层实现，后续 `forward` 时的寄存器/数据访问会失败或行为未定义。

---

### 4.2 SoC 寄存器 zone 与基地址（含 arm32/arm64 差异）

#### 4.2.1 概念说明

走 SoC 接口时，要告诉库两件事：

- **寄存器 zone**：TPU 的**控制寄存器**在 CPU 物理地址空间的哪里、有多大一坨。用结构体 `EEPTPU_REG_ZONE` 描述，它有三个字段：`core_id`（第几个 TPU 核，V3+ 是多核架构，单核使用就填 0）、`addr`（寄存器块的物理地址）、`size`（要映射多少字节）。一颗 TPU 核对应一个 zone；多核就 push 多个 zone。
- **数据基地址**：TPU 读写张量（输入图像、中间特征图、输出结果）所在的 DDR 物理地址。`set_base_address` 把这个地址告诉库，库再把它写进 TPU 硬件 / bin 的地址表，于是 TPU 的 DMA 引擎就知道去 DDR 的哪里取数、往哪里写结果。注意这是**CPU 与 TPU 都能看到的同一块 DDR 物理地址**——这正是 u1-l3 里「数据通路」的落点。

#### 4.2.2 核心流程

classify 的 SoC 分支里，用 `#if 1` 又分了 arm64 与 arm32 两套地址：

```
SoC 分支：
  #if 1   // arm64（当前启用）
      reg_zone = {core_id=0, addr=0xA0000000, size=0x1000}     # 寄存器 4KB
      #（可选）reg_zone = {core_id=1, addr=0xA0040000, size=0x1000}  # 第二个核
      set_base_address(0x40000000, 0x40000000, 0x40000000, 0x40000000)  # 数据在 DDR
  #else   // arm32（当前是死代码）
      reg_zone = {core_id=0, addr=0x43C00000, size=0x1000}
      membase = 0x30000000
      set_base_address(membase, membase, membase, membase)
  #endif
```

两个关键数字先算清楚（十六进制换算）：

- 寄存器 zone 的 `size = 0x1000`：

\[
   \mathrm{0x1000} = 4096\ \text{字节} = 4\ \text{KB}
\]

 也就是说软件只映射了 4KB 的寄存器窗口。注意这与 u1-l4 里「assign_bd_address 给每个 IP 分配 256KB（`0x40000`）地址窗口」并不矛盾：硬件层面预留的地址**窗口**是 256KB，而寄存器**实际占用**只有 4KB，软件 `mmap` 时映射够用的 4KB 即可。

- 两个核之间的地址差：`0xA0040000 - 0xA0000000`：

\[
   \mathrm{0x00400000...} \;\Rightarrow\; \mathrm{0xA0040000} - \mathrm{0xA0000000} = \mathrm{0x40000} = 256\ \text{KB}
\]

 这恰好等于一个核的 256KB 地址窗口，说明 **每个 TPU 核独占一个 256KB 的地址窗口**，多核就是把窗口依次排开。这是 u7-l1「多核多实例」会用到的事实。

#### 4.2.3 源码精读

classify 的整个 SoC 分支：

[sdk/demo/classify/main.cpp:51-72](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L51-L72) — SoC 分支，用 `#if 1/#else` 区分 arm64 / arm32。

arm64 子分支（当前启用）：

[sdk/demo/classify/main.cpp:57-64](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L57-L64) — `core_id=0` 的寄存器在 `0xA0000000`，`size=0x1000`；下一行注释掉的 `core_id=1` 在 `0xA0040000`；数据基地址全部填 `0x40000000`。

arm32 子分支（当前为死代码）：

[sdk/demo/classify/main.cpp:66-71](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L66-L71) — `core_id=0` 的寄存器在 `0x43C00000`，数据基地址 `membase = 0x30000000`。

> **关于 arm32 与 arm64 地址为什么不同**：这是 **芯片家族的 PS 物理地址地图**决定的，不是随便挑的。
>
> - **arm64 = Zynq UltraScale+（ZynqMP）**：PS 通过 HPM0_FPD（高性能主端口）访问 PL，PL 的 AXI 从机被映射进 `0xA000_0000` 起的窗口。所以第一个 AXI 从机（TPU 控制寄存器）落在 `0xA0000000`，这正是 u1-l4 里 `assign_bd_address` 给 `EEP_TPU_0` 的 `s00_axi` 分配的地址。
> - **arm32 = Zynq-7000**：这是 32 位的上一代 Zynq（Cortex-A9），PS 的 GP 主端口把 PL 映射进 `0x4000_0000` 起的区域，工程里第一个 AXI 从机被放到偏移 `0x03C0_0000` 处，于是 TPU 寄存器落在 `0x4000_0000 + 0x03C0_0000 = 0x43C0_0000`。
>
> 也就是说：**`0xA0000000` vs `0x43C00000` 的差异，本质是「换了芯片家族，PS 给 PL 开的地址窗口起点变了」**；具体落在窗口内的哪个偏移，由 Vivado 工程的 `assign_bd_address` 决定（详见 u1-l4）。数据基地址 `0x40000000`（arm64）/ `0x30000000`（arm32）则都落在 DDR 低 2GB 内，是 demo 选定的张量缓冲区物理地址（精确的预留/映射机制由闭源库内部处理，待确认）。

> ⚠️ **一个真实的源码陷阱**：arm32 子分支里用到了变量 `membase`（第 69 行），但整个 `classify/main.cpp` 里**并没有声明** `membase`。这段代码之所以能编译过，是因为它被包在 `#if 1 ... #else ... #endif` 里，而 `#if 1` 恒为真，arm32 的 `#else` 块是**死代码、根本不参与编译**。如果你真的想切到 arm32，光把 `#if 1` 改成 `#if 0` 是不够的——还得先补一行 `unsigned int membase = 0x30000000;` 之类的声明，否则会报 `'membase' was not declared`。这提醒我们：**被条件编译屏蔽的代码不等于「可用」的代码**。

#### 4.2.4 代码实践（本讲主实践任务）

**实践目标**：在 classify 的 `eeptpu_init` 里，分别列出 SoC 与 PCIE 两条分支设置了哪些参数；并解释 arm32 与 arm64 的寄存器 zone 地址为何不同。

**操作步骤**：

1. 打开 `sdk/demo/classify/main.cpp`，对照下表把 `eeptpu_init`（第 29–90 行）两条分支填空：

   | 配置项 | SoC（arm64） | SoC（arm32） | PCIE |
   | --- | --- | --- | --- |
   | 接口信息 | 无 | 无 | `set_interface_info_pcie(三个 xdma 设备)` |
   | `mem_base_addr` | 无 | 无 | `set_tpu_mem_base_addr(0x00000000)` |
   | reg_zone `addr` | `0xA0000000` | `0x43C00000` | `0x00040000` |
   | reg_zone `size` | `0x1000` | `0x1000` | `256*1024` |
   | `set_base_address` | `0x40000000`×4 | `0x30000000`×4 | `0x0`×4 |

2. 针对 arm32/arm64 的地址差异，写出你的解释（参考 4.2.3 末尾的「关于 arm32 与 arm64 地址为什么不同」）。
3. 用计算器验证：`0xA0040000 - 0xA0000000 = 0x40000 = 262144` 字节，确认「每核 256KB 窗口」的说法。

**需要观察的现象**：你会看到 SoC 分支**没有**调用 `set_interface_info_pcie` 和 `set_tpu_mem_base_addr`——这两个是 PCIE 专属；SoC 分支也**没有**调用 `set_base_address(0,0,0,0)`，而是填了真实的 DDR 地址。

**预期结果**：完成上表即得到两条分支的完整对照；地址差异的解释应落到「芯片家族（ZynqMP vs Zynq-7000）的 PS 物理地址地图不同 + `assign_bd_address` 的具体放置」这一点上。

> 本实践为源码阅读 + 推理型，无需硬件；若要在真实 arm32 板卡上验证 `0x43C00000`，需自行准备 Zynq-7000 板卡（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SoC 模式下寄存器 zone 的 `size` 只填 `0x1000`（4KB），而不是整个 256KB 窗口？

> **答案**：硬件虽然预留了 256KB 的地址窗口（`assign_bd_address` 给的），但 TPU 实际的控制寄存器只占其中靠前的 4KB。`mmap` 时只需映射真正用到的 4KB 就够了，映射整块 256KB 既浪费虚拟地址空间也无意义。

**练习 2**：如果要把 classify 从「单核」改成「双核」运行（在同一进程里用两个核），SoC arm64 分支要怎么改？

> **答案**：把第 59 行那行被注释的 `core_id=1, addr=0xA0040000, size=0x1000` 取消注释，再 `push_back` 进 `regzones`。这样 `regzones` 里就有两个 zone（core 0 和 core 1），库据此知道有两个核可用。这正是 u7-l1「多核多实例」的基础。

---

### 4.3 PCIE/XDMA：三个字符设备各司其职

#### 4.3.1 概念说明

走 PCIE 接口时，主机 CPU 不再直接读写物理地址，而是通过 **Xilinx XDMA** 这座「PCIe↔AXI 的桥」。Xilinx 提供开源的 [dma_ip_drivers/XDMA](https://github.com/Xilinx/dma_ip_drivers) 内核驱动，加载后会生成一组字符设备。demo 用到的有三个：

- `/dev/xdma0_user`：**控制通道**（user low-speed）。用来读写 TPU 的控制/状态寄存器——对应 SoC 模式里「写物理地址 `0xA0000000`」那件事，只不过这里改成「往 `xdma0_user` 文件读写」。
- `/dev/xdma0_h2c_0`：**host-to-card DMA**（h2c）。主机把张量数据（输入图像等）从主机内存搬到卡上内存，方向是 主机→卡。
- `/dev/xdma0_c2h_0`：**card-to-host DMA**（c2h）。把 TPU 算完的结果从卡上内存搬回主机，方向是 卡→主机。

一句话：**`xdma0_user` 管命令（寄存器），`xdma0_h2c_0` 管送数据进去，`xdma0_c2h_0` 管取结果出来。**

#### 4.3.2 核心流程

PCIE 分支的配置顺序：

```
PCIE 分支：
  1. set_interface_info_pcie("/dev/xdma0_user",        # 控制通道（寄存器）
                             "/dev/xdma0_h2c_0",       # 主机→卡 DMA
                             "/dev/xdma0_c2h_0")       # 卡→主机 DMA
  2. set_tpu_mem_base_addr(0x00000000)                  # 卡侧内存基址偏移 0
  3. reg_zone = {core_id=0, addr=0x00040000, size=256*1024}   # 寄存器在 xdma user 空间内的偏移
  4. set_base_address(0x0, 0x0, 0x0, 0x0)               # 基地址全 0（寻址交给 xdma）
```

这里两个数字和 SoC 截然不同，要点：

- **reg_zone `addr=0x00040000`、`size=256KB`**：在 PCIE 模式下，`addr` 不再是 CPU 物理地址，而是「寄存器块在 xdma user 通道（BAR）内的偏移」。`0x00040000` 正好等于 256KB，搭配 `size=256*1024`，描述的是卡端 BAR 里从偏移 256KB 开始、长 256KB 的那段寄存器窗口。
- **`set_base_address(0,0,0,0)`** 与 **`set_tpu_mem_base_addr(0)`**：PCIE 模式下，卡上内存的具体物理地址由 xdma 引擎在传输时处理，软件侧只需把基址偏移设成 0，所以四个槽位都填 0（`set_base_address` 的 4 个参数对应 4 个基地址槽位，可能对应不同 DDR bank 或 input/output/weight 区域，精确语义由闭源库内部解释，待确认）。

#### 4.3.3 源码精读

classify 的 PCIE 分支：

[sdk/demo/classify/main.cpp:35-50](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L35-L50) — 完整的 PCIE 分支：先 `set_interface_info_pcie` 绑定三个设备文件，再设内存基址、reg zone、基地址。

三个 xdma 设备文件的绑定：

[sdk/demo/classify/main.cpp:38](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L38) — `eeptpu_set_interface_info_pcie("/dev/xdma0_user", "/dev/xdma0_h2c_0", "/dev/xdma0_c2h_0")`。这一句是 PCIE 模式的「命门」——少了它，库根本不知道去哪找 xdma。

yolo demo 的 PCIE 分支与 classify 完全一致（参数一字不差）：

[sdk/demo/yolo/main.cpp:99](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L99) — yolo 里同样绑定这三个设备文件，说明这是 XDMA 卡的**通用契约**，与具体网络无关。

#### 4.3.4 代码实践

**实践目标**：把 PCIE 模式下「软件的一次 forward」在主机侧拆成对三个设备文件的操作。

**操作步骤**：

1. 阅读 PCIE 分支，把三个设备文件按「寄存器/送数据/取结果」三类对号入座。
2. 设想一次完整推理（先 `set_input` 再 `forward` 再读结果），按时间顺序列出主机会用到哪个设备文件：
   - 启动/查询状态 → `xdma0_user`（写控制寄存器、读状态寄存器）；
   - 送输入张量 → `xdma0_h2c_0`；
   - 取输出张量 → `xdma0_c2h_0`。
3. 对比 SoC 模式：同样的三件事，SoC 下分别对应「写物理地址 `0xA0000000`」「写 DDR `0x40000000`」「读 DDR 输出区」。

**需要观察的现象**：你会发现两种接口的**逻辑步骤完全对称**，只是「搬运手段」不同——SoC 靠内存映射直接读写地址，PCIE 靠读写三个 xdma 字符设备。

**预期结果**：能画出「SoC 物理地址 ↔ PCIE xdma 设备」的对应表：

| 逻辑动作 | SoC 实现 | PCIE 实现 |
| --- | --- | --- |
| 读写控制寄存器 | 读写物理地址 `0xA0000000` | 读写 `/dev/xdma0_user` |
| 送输入张量到 TPU | 写 DDR `0x40000000` | 写 `/dev/xdma0_h2c_0` |
| 取输出张量回主机 | 读 DDR 输出区 | 读 `/dev/xdma0_c2h_0` |

> 本实践为推理 + 阅读型；在真实 PCIe 卡上验证需要先装好 Xilinx xdma 驱动并出现这三个设备节点（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：PCIE 模式下，如果 `/dev/xdma0_c2h_0` 这个节点不存在（驱动没装或卡上没有这个通道），推理会在哪一步失败？

> **答案**：能正常 `set_input`（用 h2c 送数据）、能启动 TPU（用 user 写寄存器），但在**读结果**这一步会失败——因为取结果靠的是 `c2h`（card-to-host），通道不存在就读不回输出张量。

**练习 2**：为什么 PCIE 模式下 `set_base_address` 全填 0，而 SoC 模式要填 `0x40000000`？

> **答案**：SoC 模式下，CPU 和 TPU 共享同一块 DDR 物理地址，必须约定一个具体的基地址（`0x40000000`）作为张量缓冲区；PCIE 模式下，主机看到的不是卡上真实物理地址，而是 xdma 引擎管理的一段地址空间，基址偏移由 xdma 在传输时处理，软件把基址设成 0 即可（卡侧内存基址另由 `set_tpu_mem_base_addr(0)` 设定）。

---

### 4.4 切换接口的两种写法：运行时变量 vs 编译时宏 `INTERFACE_PCIE`

#### 4.4.1 概念说明

同样是「在 SoC 和 PCIE 之间切换」，仓库里有两种写法：

- **运行时切换（classify）**：用一个普通变量 `eep_interface_type` 保存当前选择，`eeptpu_init` 用 `if/else if` 在程序运行时分叉。优点是**一个二进制能跑两种硬件**，改一行变量重跑即可；缺点是两条分支的代码都编进二进制，体积稍大，且运行时多一次判断。
- **编译时切换（yolo / multi_bins_test）**：用预处理宏 `INTERFACE_PCIE`，靠 `#if defined(INTERFACE_PCIE)` 在**编译期**就决定走哪条分支，另一条分支根本不进二进制。优点是**死代码被剔除、二进制更干净**；缺点是切换必须重新编译（要给编译器加 `-DINTERFACE_PCIE`）。

两者本质都在回答同一个问题：「这块板子是 SoC 形态还是 PCIe 卡形态？」只是回答的**时机**不同——一个在运行时，一个在编译时。

#### 4.4.2 核心流程

classify（运行时）：

```
static int eep_interface_type = eepInterfaceType_SOC;      // 改这行即切换
//static int eep_interface_type = eepInterfaceType_PCIE;
...
eeptpu_init(interface_type, path_bin):
    if (interface_type == eepInterfaceType_PCIE) { ... }
    else if (interface_type == eepInterfaceType_SOC)  { ... }
```

yolo（编译时）：

```
#if defined(INTERFACE_PCIE)
    static int eep_interface_type = eepInterfaceType_PCIE;
#else
    static int eep_interface_type = eepInterfaceType_SOC;   // 默认
#endif
...
eeptpu_init():
    #if defined(INTERFACE_PCIE)
        ... // 只编 PCIE 分支
    #else
        ... // 只编 SoC 分支
    #endif
```

注意 yolo 的 `eeptpu_init` 里第 97 行还有一句 `if (interface_type != eepInterfaceType_PCIE) return -1;`——这是编译时切换的**配套自检**：既然用宏选了 PCIE，运行时就强制要求接口必须是 PCIE，否则直接返回错误。

#### 4.4.3 源码精读

yolo 顶部的宏开关：

[sdk/demo/yolo/main.cpp:83-87](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L83-L87) — 用 `#if defined(INTERFACE_PCIE)` 在编译期决定 `eep_interface_type` 的取值。没有定义该宏时默认 SoC。

yolo 的 `eeptpu_init` 同样用宏分叉：

[sdk/demo/yolo/main.cpp:95-122](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/yolo/main.cpp#L95-L122) — `#if defined(INTERFACE_PCIE)` 选 PCIE 分支，`#else` 选 SoC 分支。与 classify 相比，yolo **没有** arm32 的 `#else` 子分支，SoC 分支只保留了 arm64 一套地址（`0xA0000000` / `0x40000000`）。

对比 classify 的运行时写法：

[sdk/demo/classify/main.cpp:17-18](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/main.cpp#L17-L18) — 普通变量 + 注释行，切换靠改源码里的赋值，不需要重新加宏。

那么「`INTERFACE_PCIE` 这个宏从哪来」？答案是：**由编译命令注入**。看 classify 的编译脚本：

[sdk/demo/classify/compile.sh:35-37](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/demo/classify/compile.sh#L35-L37) — `cflags` 里目前**只有**头文件包含路径（`-I...`），**并没有** `-DINTERFACE_PCIE`。所以默认编译出的 demo 一定是 SoC 模式。要用 PCIE，就得在 `cflags` 后面追加 `-DINTERFACE_PCIE`。

> 注意：classify 用的是运行时变量，所以 classify 的 `compile.sh` 本来就不需要这个宏；但 yolo / multi_bins_test 用的是编译时宏，要切 PCIE 就**必须**改它们各自的 `compile.sh`（结构完全一样），加上 `-DINTERFACE_PCIE` 后重新编译。

#### 4.4.4 代码实践

**实践目标**：把一个默认 SoC 的 demo（以 yolo 为例）改成 PCIE 编译，并理解改完之后哪些代码会消失。

**操作步骤**：

1. 打开 `sdk/demo/yolo/compile.sh`（结构与 classify 的完全一致），找到 `cflags=...` 那一行。
2. 在 `cflags` 末尾追加 ` -DINTERFACE_PCIE`，例如：
   ```bash
   cflags="-I./ -I../libs/${pf}/eep/include -I../common/npy -I../common/eepimg_v0.2.6/ -DINTERFACE_PCIE"
   ```
   （这是**示例修改**，仅演示注入宏的位置；仓库原版并无此宏。）
3. 重新执行 `./compile.sh 64` 编译。
4. 对照 `sdk/demo/yolo/main.cpp:83-87` 与 `:95-122`，回答：编译后二进制里还保留 SoC 分支的代码吗？

**需要观察的现象**：加上 `-DINTERFACE_PCIE` 后，预处理器会把 `#else` 分支（SoC 分支）整段剔除，最终的 `demo` 二进制里**只有** PCIE 的初始化路径；运行时也会打印 `Interface type: PCIE`。

**预期结果**：能在串口/终端看到 `Interface type: PCIE`；若运行环境不是 PCIe 卡（比如还在 ZynqMP 板卡上），则会在 yolo 的第 97 行自检处直接返回 `-1` 并报错。

> 本实践为**配置修改 + 重新编译**型，需要交叉编译环境；运行验证需要真实 PCIe 加速卡 + 已装 xdma 驱动（待本地验证）。仅做编译实验（不运行）在 x86 主机上即可观察到「PCIE 分支被编入、SoC 分支被剔除」。

#### 4.4.5 小练习与答案

**练习 1**：classify 的 `compile.sh` 里**没有** `-DINTERFACE_PCIE`，为什么 classify 依然能在 PCIE 卡上跑？

> **答案**：因为 classify 用的是**运行时变量** `eep_interface_type`（第 17 行），它的 SoC/PCIE 两条分支都被无条件编译进二进制；切到 PCIE 只需把第 17、18 行互换（让变量取 `eepInterfaceType_PCIE`）再重编一次，并不依赖任何编译宏。

**练习 2**：运行时切换和编译时切换，各适合什么场景？

> **答案**：
> - **运行时切换**适合「同一个二进制要在多种硬件上跑、或运行时才知道硬件形态」的场景，灵活但二进制略大。
> - **编译时切换**适合「目标硬件固定、想抠掉无用代码减小体积、避免误用」的场景，比如发货到 PCIe 卡的固件就只编 PCIE 分支，配合第 97 行的自检还能防止在错误硬件上误运行。

---

## 5. 综合实践

**任务**：假设你拿到一块**未知形态**的硬件，要部署 classify demo。请设计一套判断与配置流程，把本讲四个模块串起来。

**要求**：

1. **判形态**：先确定它是 SoC 板卡还是 PCIe 卡。你会用什么线索判断？（提示：PCIE 卡上 `ls /dev/xdma0_*` 会出现三个设备节点；SoC 板卡则不会，但能在 `/dev/mem` 里映射到 `0xA0000000`。）
2. **选写法**：如果你希望「一个二进制两种卡都能跑」，应该模仿 classify（运行时变量）还是 yolo（编译时宏）？为什么？
3. **填参数**：分别写出两种形态下 `eeptpu_init` 要填的关键参数（接口类型、reg_zone 的 `addr/size`、`set_base_address`、是否需要 `set_interface_info_pcie`）。
4. **避陷阱**：如果硬件其实是 arm32（Zynq-7000）板卡，你想启用 classify 的 arm32 分支，除了把 `#if 1` 改成 `#if 0`，还需要补什么？（回顾 4.2.3 的 `membase` 陷阱。）

**预期产出**：一张「形态 → 写法 → 参数 → 注意事项」的部署清单。完成后，你应该能针对任意一种交付硬件，快速给出 classify demo 的正确初始化配置。

> 本综合实践为设计 + 阅读型，不依赖硬件即可完成方案的撰写；实际部署需在对应硬件上验证（待本地验证）。

## 6. 本讲小结

- TPU 有两种接入方式：**SoC（AXI 内存映射）**——TPU 在 ZynqMP 芯片内，CPU 直接读写物理地址；**PCIE（XDMA）**——TPU 在 PCIe 卡上，主机经 `/dev/xdma0_user`（寄存器）、`/dev/xdma0_h2c_0`（送数据）、`/dev/xdma0_c2h_0`（取结果）三个字符设备访问。
- 两种接口的逻辑步骤**完全对称**，只是搬运手段不同：写控制寄存器、送输入张量、取输出张量。
- SoC 的寄存器 zone `addr`：arm64（ZynqMP）是 `0xA0000000`、arm32（Zynq-7000）是 `0x43C00000`，差异源于**芯片家族的 PS 物理地址地图不同**，并由 Vivado 的 `assign_bd_address` 定死；`size=0x1000` 是软件实际映射的 4KB 寄存器窗口。
- PCIE 的 reg_zone `addr=0x00040000`、`size=256KB`，是寄存器块在 xdma user 通道内的偏移；`set_base_address(0,0,0,0)` 与 `set_tpu_mem_base_addr(0)` 把寻址交给 xdma 引擎。
- 切换接口有两种写法：classify 用**运行时变量**（一个二进制跑两种硬件），yolo/multi_bins_test 用**编译时宏 `INTERFACE_PCIE`**（`compile.sh` 里加 `-DINTERFACE_PCIE`，死代码被剔除）。
- classify 的 arm32 子分支里 `membase` 未声明——因为被 `#if 1` 屏蔽成死代码；真要切 arm32 必须先补声明，这是「条件编译不等于可用代码」的鲜活例子。

## 7. 下一步学习建议

- **向深走（寄存器协议）**：本讲只讲了「接口怎么选、地址怎么填」，但 SoC 模式下 CPU 具体往 `0xA0000000` 写哪些寄存器、怎么启动 TPU、怎么轮询完成，是裸机 standalone 的话题。建议进入 U4 单元，重点读 `sdk/standalone/src/eeptpu/eeptpu_sa.cpp` 与 `config.h` 里的 `EEPTPU_*_REG` 宏（u4-l2 / u5-l1）。
- **向宽走（多核多实例）**：本讲提到 `core_id=1` 的 zone（`0xA0040000`）。想看一个进程里同时跑两个 TPU 实例、占用两个核的真实例子，直接读 `sdk/demo/multi_bins_test/main.cpp`（u7-l1）。
- **横向印证（地址契约）**：想确认 `0xA0000000` 这个「魔法地址」确实来自硬件设计，回头对照 u1-l4 里 `system_rtl_*.tcl` 的 `assign_bd_address`，你会看到软硬件用的是同一张地址表。
