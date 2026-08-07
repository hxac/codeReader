# 软件架构、bare-metal 启动与内存映射

## 1. 本讲目标

本讲进入 Unit 6「软件控制面」的第一站。学完本讲后，读者应该能够：

- 说出 wireguard-fpga 控制面（跑在 picoRV32 软 CPU 里的 C/C++ 固件）由哪些组件拼成，以及每个组件在 WireGuard 的 Noise 协议里扮演什么角色。
- 读懂 `boot_crt.s` 这段「裸机启动汇编（CRT）」：从复位到进入 `main()` 之间，CPU 到底做了哪几件事，尤其理解哈佛架构下 `.data` 段如何从 IMEM「搬运」到 DMEM。
- 读懂 `link_map.lds` 链接脚本对 IMEM / DMEM / CSR 三段地址空间的划分，并能把它与 `csr.rdl` 的 `wireguard addrmap`、以及自动生成的 HAL 硬编码基址三方对齐确认一致。

本讲是承接 u2-l4（SoC 互联 fabric 与地址译码）与 u3-l1（SystemRDL 寄存器规格）的「软件侧落地篇」：u2-l4 讲了 CPU 怎么经总线找到 DMEM 与 CSR，u3-l1 讲了寄存器地图怎么用 SystemRDL 描述，本讲则回答——**软件这一侧的代码、数据、外设究竟被放在哪些地址，上电后又是怎么跑起来的**。

## 2. 前置知识

- **控制面 vs 数据面（u2-l1）**：系统分两层。控制面是软 CPU 里跑的固件，处理低频但逻辑复杂的握手与路由管理；数据面是 RTL，做线速加解密转发。本讲讲的是控制面固件本身。
- **bare-metal / freestanding（裸机/独立环境）**：这块 CPU 上没有 Linux、没有标准 C 库（libc）、没有动态加载器。程序直接从地址 0 开始执行，`main()` 返回后无处可去。一切「运行前准备」（清 BSS、搬数据、设栈、设全局指针）都得自己写一小段汇编完成，这段汇编就叫 **CRT（C Run Time）**。
- **哈佛架构（Harvard）vs 冯·诺依曼（Von-Neumann）**：哈佛架构把「指令存储器 IMEM」和「数据存储器 DMEM」物理分开；冯·诺依曼则统一在一段内存里。本项目默认用哈佛（`-DHARVARD=1`），所以代码放 IMEM、变量放 DMEM，两边各是一块 BRAM。
- **链接脚本（linker script, `.lds`）**：告诉链接器「代码段 `.text`、只读数据 `.rodata`、已初始化数据 `.data`、零初始化数据 `.bss`、堆 `.heap`、栈 `.stack`」分别映射到哪段物理地址。链接脚本必须和 RTL 设计的存储器地址对齐，否则 CPU 取指/取数会取到错误的地址。
- **LMA vs VMA**：VMA（Virtual Memory Address）是程序运行时访问的地址；LMA（Load Memory Address）是程序映像在「存储介质」里实际摆放的地址。哈佛架构下两者常常不同——`.data` 运行时在 DMEM（VMA），但烧写映像里它紧跟在 `.text` 后面放在 IMEM（LMA），上电时再由 CRT 搬过去。这是本讲的一个难点，后面会详讲。
- **CSR / HAL（u3-l1、u3-l2）**：CSR 是软硬件的唯一桥梁；HAL 是 PeakRDL 自动生成的「层级指针访问」封装，让固件能写 `csr->dpe->fcr->pause(1)` 这样的链式调用，底层其实是指针加固定地址偏移。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `2.sw/`、`3.build/` 下）：

| 文件 | 作用 |
|------|------|
| [2.sw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md) | 控制面软件架构与运行原理的权威说明，列出全部组件及其职责 |
| [2.sw/boot_crt.s](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s) | 裸机启动汇编（CRT），从 `_boot_crt` 到 `call main` 之间的全部初始化 |
| [2.sw/link_map.lds](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds) | 链接脚本，定义 IMEM/DMEM 区间与各段（.text/.data/.bss/.heap/.stack）布局 |
| [2.sw/app/wireguard_libs.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/wireguard_libs.h) | 裸机 `malloc`/`new` 声明，体现「无 libc」约束 |
| 2.sw/app/wireguard_libs.cpp | `malloc` 的具体实现（bump allocator） |
| 2.sw/app/main.cpp | 控制面入口 `int main(void)`，组合所有组件 |
| 3.build/MakefileSW | SW 构建编排，决定链接脚本/启动汇编/交叉编译如何串起来 |
| 3.build/csr_build/csr.rdl | SystemRDL 真源，其末尾的 `wireguard addrmap` 给出 IMEM/DMEM/CSR 的地址 |
| 3.build/csr_build/generated-files/csr_hw.h | PeakRDL 生成的硬件 HAL，硬编码了 CSR 基址 `0x20000000` |

## 4. 核心概念与源码讲解

### 4.1 控制面组件总览

#### 4.1.1 概念说明

控制面固件是一个**没有操作系统、没有标准库**的 C/C++ 程序，跑在 picoRV32（一款开源 32 位 RISC-V 软核）上。它的核心是 **WireGuard Agent**——实现 Noise 协议握手流程，协商出会话密钥后，把密钥和路由信息写进数据面的表里，之后真正的用户数据包就完全由硬件线速转发，不再经过 CPU。

围绕 WireGuard Agent 的是一组**密码学原语**和**支撑组件**。这些组件几乎都来自公开的标准实现（TweetNaCl、RFC 8439、RFC 7693、WireGuard 内核的 HKDF 逻辑），被裁剪成「可移植、无动态分配、适合裸机」的形态。

> 为什么控制面要用「软件 + 软 CPU」而不是全硬件？因为握手流程逻辑复杂但频率极低（几秒到几分钟一次密钥轮换），用软件写又灵活又省面积；而线速转发用软件跑不动，必须用硬件。这就是 u2-l1 讲的「软硬分工」动机。

#### 4.1.2 核心流程

控制面的组件可以分成三类，对应 Noise 握手的三类需求：

```
                  ┌─────────────────────────────────────────┐
   WireGuard      │  ① 密钥协商原语                          │
   Agent (主)  ──►│     curve25519 (X25519 / ECDH)          │
   (handshake)    │     hkdf        (Noise KDF, 派生会话密钥)│
                  │     blake2s     (哈希 + HMAC 基石)       │
                  │     random      (rdcycle 熵源)          │
                  ├─────────────────────────────────────────┤
                  │  ② 数据保护原语（控制面自用，少量加解密） │
                  │     chacha20 + poly1305 → chacha20poly1305 (AEAD)
                  ├─────────────────────────────────────────┤
                  │  ③ I/O 与支撑                            │
                  │     uart (CLI)、ethernet/network (收发包)│
                  │     timer (rdcycle 延时)、wireguard_libs (malloc)
                  └─────────────────────────────────────────┘
                              │
                              ▼  握手完成后，经 HAL/CSR
                   Routing DB Updater ──► 写 routing_table / cryptokey_table
                                        （数据面随即用这些表线速转发）
```

注意：第 ② 类的 ChaCha20-Poly1305 在控制面里只用于少量报文（例如 Cookie Reply 防 DoS）；真正的线速数据加解密由 Unit 5 的硬件流水线承担。控制面这份 C 实现是「软件参考 + 握手期自用」。

#### 4.1.3 源码精读

组件清单直接来自 [2.sw/README.md:L10-L23](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L10-L23)——这段逐条列出了 WireGuard Agent 及其辅助组件，是本节的权威索引。

把这份清单对应到 `2.sw/app/` 下的真实源码文件，得到下表（每一项都是 `2.sw/app/` 下实际存在的文件）：

| 组件（README 描述） | 源文件 | 在 Noise 协议中的角色 |
|---|---|---|
| Curve25519 (X25519 ECDH) | `curve25519.c` + `tweetnacl_x25519.c` | 用私钥+对方公钥算出共享密钥 |
| ChaCha20-Poly1305 (AEAD) | `chacha20.c` + `poly1305.c` + `chacha20poly1305.c` | 加密 + 认证，防重放 |
| BLAKE2s | `blake2s.c` | 哈希、HMAC、KDF 的底层基石（RFC 7693） |
| HKDF (Noise KDF) | `hkdf.c` | 把 ECDH 结果扩展成至多 3 个 32 字节密钥 |
| RNG | `random.c` | 用 `rdcycle` 取熵，初始化 DH 私钥、生成 peer ID |
| Timer | `timer.c` | 基于 `rdcycle` 的 `delay_us`/`delay_ms`，用于 rekey/retry/keepalive |
| 网络 / CLI | `ethernet.c` + `network.c` + `string_bare.c` + `uart.c` | 收发 IP/UDP/ARP/ICMP、提供 CLI（无 libc 字符串） |
| HAL/CSR Driver | （生成）`csr_hw.h` | 经 CSR 读写 DPE 寄存器与表 |
| WireGuard Agent / Routing DB Updater | `main.cpp` | 握手主逻辑 + 把表部署到数据面 |

几个体现「裸机友好」的设计要点，README 里有明确交代：

- **无动态内存分配**：每个密码原语都强调 "No dynamic memory allocation" 与 "Suitable for bare-metal environments"，例如 [2.sw/README.md:L46-L50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L46-L50)（curve25519 的实现说明）。需要堆的地方，用 `wireguard_libs.cpp` 里自写的极简 `malloc`（见 4.2.3）替代 libc。
- **熵源是 `rdcycle`**：没有硬件随机数发生器时，RNG 用 RISC-V 的周期计数器 `rdcycle` 取熵、再经 BLAKE2s 混合，见 [2.sw/README.md:L156-L161](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L156-L161)。它不是密码学安全的，但作为 Noise 内部的输入源够用。
- **常时间标量乘**：curve25519 "Constant-time scalar multiplication with respect to the secret scalar"，防止私钥通过时序侧信道泄露（[2.sw/README.md:L44-L50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L44-L50)）。

控制面的入口在 `main.cpp` 的 `main()`，它先把所有组件「装配」起来：创建 CSR HAL 根对象、校验硬件 ID、打印 banner、初始化网络，然后进入主循环，见 [2.sw/app/main.cpp:L784-L815](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L784-L815)。具体的握手与表更新细节留到 u6-l4 讲，本节只需建立「组件全景」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 README 的组件清单与 `2.sw/app/` 的真实文件一一对应，确认「清单上每一条都能找到一个源文件」。
2. **操作步骤**：打开 [2.sw/README.md:L10-L23](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L10-L23)，对照本节上面的表格，逐条在 `2.sw/app/` 下找到对应 `.c/.h`。
3. **需要观察的现象**：README 提到的组件是否都有对应实现文件；注意 `string_bare.c` 这种「自实现字符串」的文件——它说明项目完全不用 libc。
4. **预期结果**：11 条组件清单可对应到约 13 个源文件（部分组件如 AEAD 拆成 chacha20+poly1305+聚合层三个文件）。
5. 待本地验证（无需运行，纯文件对照）。

#### 4.1.5 小练习与答案

**练习 1**：为什么控制面不直接用 Linux 内核的 WireGuard 实现，而要自己写一套裸机 C？
**答案**：因为 picoRV32 是一颗极简软核，上面没有 Linux、没有 libc、没有 MMU。控制面固件是 freestanding 程序，必须把密码原语裁剪成「无动态分配、无系统调用、可移植」的形态才能跑。这也正是 README 反复强调 "bare-metal" 的原因。

**练习 2**：BLAKE2s 在 Noise 协议里被 HKDF 和 HMAC 复用，请说出它「向下」依赖什么、「向上」被谁依赖。
**答案**：BLAKE2s「向下」只依赖显式的 32 位整数运算（移位、异或、轮转），不依赖任何平台 intrinsic；「向上」被 HMAC（在 `hkdf.c` 里）和直接哈希调用依赖，是整个密钥派生链的基石。

---

### 4.2 bare-metal 启动流程

#### 4.2.1 概念说明

在有操作系统的环境里，`main()` 之前的事（清 BSS、搬 `.data`、设栈、调用全局构造函数）由 glibc 的 `_start` 和 CRT 完成。裸机环境没有这些东西，所以项目自带一段启动汇编 [2.sw/boot_crt.s](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s)，承担最小化的 CRT 职责。

文件名用大写 `.S`（不是 `.s`）是 GCC 约定：**大写 `.S` 会先过 C 预处理器**，所以文件里能用 `#ifdef FLASH_BOOT`、`#ifdef TESTCODE` 这类宏做条件编译（见文件头注释 [2.sw/boot_crt.s:L37-L42](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L37-L42)）。

启动入口符号是 `_boot_crt`（[2.sw/boot_crt.s:L66](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L66)），它会被链接器放在 IMEM 的地址 0，复位后 CPU 从这里取第一条指令。

#### 4.2.2 核心流程

当前默认构建（`-DTCM=1 -DHARVARD=1`，无 `FLASH_BOOT`）下，启动流程分为「公共三步」：

```
_boot_crt (复位入口, 地址 0)
   │
   │  (FLASH_BOOT 段被 ifdef 掉，当前不走——见 4.2.3 说明)
   │
   ├─ 步骤①  搬运 .data（哈佛架构的关键！）
   │     源: _idata_start  (LMA = 紧跟 .text 之后，在 IMEM 里)
   │     的: _data_start   (VMA = DMEM 起始)
   │     循环 lw/sw，每个字 4 字节，直到 _data_end
   │
   ├─ 步骤②  清零 .bss
   │     从 _bss_start 到 _bss_end，逐字写 0
   │     （C 标准要求未初始化全局变量初值为 0）
   │
   ├─ 步骤③  设置 C 运行所需的寄存器
   │     sp  = _stack_start   (DMEM 顶端，栈向下生长)
   │     gp  = __global_pointer$  (全局指针，--relax 优化用)
   │     a0/a1/a2 = 0         (argc/argv/envp)
   │
   └─ call main              (进入 C++ main)
        └─ loop_forever: j loop_forever   (main 返回后死循环)
```

为什么步骤①必不可少？因为本项目是哈佛架构：`.data`（已初始化的全局变量）运行时必须待在 DMEM（VMA），但烧写映像 `imem.INIT.vh` 只能初始化 IMEM。所以链接器把 `.data` 的初始值「藏」在 `.text` 后面（LMA），上电时由这段汇编搬过去。链接脚本里 `.data : AT ( _text_end )` 这一句就是干这个的（见 4.3.3）。

#### 4.2.3 源码精读

**搬运 `.data`** —— 这是整段启动汇编最值得读的部分，[2.sw/boot_crt.s:L146-L162](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L146-L162)：

- `la a0, _idata_start` 取 LMA 源地址（紧跟 `.text` 之后）；
- `la a1, _data_start` / `la a2, _data_end` 取 VMA 目标区间（在 DMEM）；
- `loop_copy_idata` 用 `lw a3,0(a0)` / `sw a3,0(a1)` 逐字搬，每搬一个字 `a0`、`a1` 各加 4，直到 `a1 >= a2`。

**清零 `.bss`** —— [2.sw/boot_crt.s:L165-L175](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L165-L175)，逻辑同上，只是源是常量 0、目标是 `[_bss_start, _bss_end)`。

> 注意 [2.sw/boot_crt.s:L113-L124](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L113-L124) 那段被 `#-NOT-NEEDED` 注释掉的「整片清零 DMEM」代码——作者特意标注「DMEM 越大越慢，会让仿真不可用」，所以只清 `.bss` 而非整片 DMEM。

**设置 sp/gp 并进入 main** —— [2.sw/boot_crt.s:L178-L197](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L178-L197)：

- `la sp, _stack_start` 设栈顶；`la gp, __global_pointer$` 设全局指针（用 `.option norelax` 防止 `la gp` 被链接器 relaxation 改写成相对 `gp` 自身的引用，这是个经典坑）。
- `call main` 进入 C++ 主函数；`loop_forever: j loop_forever` 是兜底——裸机下 `main` 不该返回，万一返回就死循环，避免 PC 跑飞。

关于 `FLASH_BOOT`：[2.sw/boot_crt.s:L68-L143](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L68-L143) 整段在 `.ifdef FLASH_BOOT` 里，且其中的「从 Flash 拷代码」还标着 `#-TODO` 未实现。当前固件是经 UART 在线烧进 IMEM（u2-l5 讲过），不走 Flash 启动，所以这段不生效。

**裸机 `malloc` 的配合**：启动没有 libc，但 C++ `new` 和部分代码仍要堆。`wireguard_libs.cpp` 提供了一个 bump allocator（线性分配、不回收），见 [2.sw/app/wireguard_libs.cpp:L47-L56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/wireguard_libs.cpp#L47-L56)：一块 32 KB 的静态数组 `heap_memory[32768]`，`malloc` 只是把指针往后挪、用 `ebreak` 在溢出时触发调试断点。声明在 [2.sw/app/wireguard_libs.h:L53-L56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/wireguard_libs.h#L53-L56)。这套实现整体被 `#ifndef VPROC` 包住——协同仿真（VProc）模式下用宿主机的真 `malloc`，只有真硬件走这个裸机版。

#### 4.2.4 代码实践（反汇编阅读型）

1. **实践目标**：在编译产物里看到「`.data` 真的从 IMEM 搬到 DMEM」。
2. **操作步骤**：执行 SW 构建（`make -f MakefileSW sw_elf`，需先跑过 CSR 构建生成 `csr_hw.h`），然后阅读 `3.build/sw_build/main.dump`（由 `objdump -drwC -S` 生成，见 [3.build/MakefileSW:L112](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/MakefileSW#L112)）。在反汇编里找到 `_boot_crt`，确认它落在最低地址（接近 0x0）。
3. **需要观察的现象**：`_boot_crt` 是否在 IMEM 起始；`loop_copy_idata` 里的源地址 `_idata_start` 是否落在 `.text` 之后、目标 `_data_start` 是否落在 `0x10000000` 一带（DMEM）。
4. **预期结果**：LMA（源）在 `0x0000xxxx`，VMA（目标）在 `0x1000xxxx`，直观证明「搬数据」跨了存储器。
5. 待本地验证（依赖交叉工具链 `riscv64-unknown-elf-` 已安装）。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 boot_crt.s 里「清零 `.bss`」那步，程序会出什么问题？
**答案**：C/C++ 标准规定未初始化的全局变量初值为 0，编译器据此把这种变量放进 `.bss`（不占映像空间）。若不清零，这些变量会是 BRAM 上电时的随机值，可能导致计数器非 0、指针乱、状态机起步错误等隐蔽 bug。

**练习 2**：为什么 `call main` 后面要跟一个 `loop_forever: j loop_forever`？
**答案**：裸机没有「进程退出」的概念，`main` 返回后 CPU 会继续取下一条指令，而那里是没有意义的内存，PC 会跑飞。死循环是个安全网。本项目里 `main` 本身就是个 `while(1)`，理论上不会返回，这个兜底是防御性编程。

---

### 4.3 内存映射：IMEM / DMEM / CSR

#### 4.3.1 概念说明

控制面 CPU 看到的是一段 32 位地址空间，被分成三个用途迥异的区域：

- **IMEM（指令存储器）**：放 `.text` 代码和 `.rodata` 常量，只读、可执行。复位从地址 0 取指。
- **DMEM（数据存储器）**：放 `.data`/`.bss`/`.heap`/`.stack`，可读可写、不可执行。
- **CSR（控制状态寄存器）**：不是普通内存，而是**内存映射 I/O（MMIO）**——访问这里的地址其实是读写硬件外设寄存器（uart、gpio、ethernet、cpu_fifo、dpe、routing/cryptokey 表）。

关键认知：**这三段地址必须由三方共同维护一致**——链接脚本（决定代码/数据放哪）、SystemRDL 真源（描述硬件存储器与 CSR 地图）、HAL（软件访问 CSR 的指针基址）。任何一方对不上，CPU 就会取错指、读错数、或访问不存在的外设。本节就是把这三方摆在一起对照。

#### 4.3.2 核心流程

把三段地址画成一张总图（当前默认构建 `-DTCM=1 -DHARVARD=1`）：

```
地址                 区域      归属           说明
─────────────────────────────────────────────────────────────────
0x0000_0000          IMEM      指令 BRAM      32 KB (8192×32b)，复位入口
0x0000_7FFF          ─────     ─────          ─ IMEM 结束 ─
   ...                                          (地址窗口保留到 64K)
0x1000_0000          DMEM      数据 BRAM      32 KB (8192×32b)，.data/.bss/heap/stack
   ...                                          (地址窗口保留到 64K)
0x2000_0000          CSR       MMIO 外设      uart/gpio/ethernet/cpu_fifo/dpe/两张表
0x2000_0000+0x400    ─ routing_table   (external regfile, 64 条目)
0x2000_0000+0x2000   ─ cryptokey_table (external regfile, 64 条目)
```

三者由不同机制落到硬件：
- IMEM/DMEM 是真实 BRAM，链接脚本和 `csr.rdl` 都声明了它们的基址与深度。
- CSR 不是一块连续 RAM，而是 `soc_fabric`（u2-l4）做的**地址译码**——CPU 给出 `addr[31:29]==1`（即 `0x2000_0000~0x3FFF_FFFF`）就命中 CSR 从口，再由 PeakRDL 生成的 `csr.sv` 细译码到具体寄存器。
- 重要：**链接脚本里没有专门的 CSR 段**。变量不会被自动放进 CSR 区；固件是通过 HAL 给的「硬编码指针」`0x20000000` 去主动访问 CSR 的。

#### 4.3.3 源码精读

**① 链接脚本定义 IMEM/DMEM 区间** —— [2.sw/link_map.lds:L63-L103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L63-L103)。在 `#if TCM` + `#if HARVARD` 分支下：

- [L73](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L73)：`IMEM (xr!w) : ORIGIN = 0x00000000, LENGTH = 64K`（可执行、只读、可写标志 `xr!w` 实际表示 execute+read）。
- [L77](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L77)：`DMEM (rw!x) : ORIGIN = 0x10000000, LENGTH = 64K`（可读写、不可执行）。

> 属性里的 `xr!w` / `rw!x` 是「权限字母 + `!` 表示去掉」，用来表达 Harvard 下「代码区不可写、数据区不可执行」的安全意图。

**② 哈佛架构的 `.data` 双地址** —— [2.sw/link_map.lds:L166-L177](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L166-L177)：

```ld
.data : AT ( _text_end )      // LMA = _text_end (在 IMEM)
{
   _data_start = .;            // VMA = DMEM 当前位置
   *(.data .data.*)
   . = ALIGN(8);
   PROVIDE( __global_pointer$ = . + 0x800 );   // gp 指向数据区中部
   *(.sdata .sdata.*)
   _data_end = .;
}
```

`AT(_text_end)` 是关键：`.data` 的运行地址（VMA）在 DMEM，但加载地址（LMA）紧跟 `.text` 之后——所以烧写映像里 `.data` 的初值在 IMEM，正是 4.2 讲的 boot_crt 搬运的来源。`_idata_start = _text_end` 这条赋值在 [L151](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L151) 处给出，与 boot_crt.s 的 `la a0, _idata_start` 对应。

**③ 栈与堆** —— [2.sw/link_map.lds:L225-L257](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L225-L257)：堆大小 `_HEAP_SIZE = 0x400`（1 KB）、栈大小 `_STACK_SIZE = 0x200`（512 B）；`_stack_start` 被设为 DMEM 末端（`ORIGIN(DMEM) + LENGTH(DMEM)`），栈从顶向下长。注意这里的「堆」是链接脚本预留的 1 KB，和 `wireguard_libs.cpp` 里 32 KB 的 `heap_memory` 静态数组是两回事——后者是一个普通全局变量，落在 `.bss`，不占链接脚本那个 `.heap` 段。

**④ 没有专门的 CSR/I/O 段** —— [2.sw/link_map.lds:L105-L110](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L105-L110) 明确写着「For simplicity, the I/O section is not explicitly defined」，并注释掉了 `IO (rw!x) : ORIGIN = 0xFFFF0000`。所以 CSR 的访问完全靠 HAL 的硬编码基址。

**⑤ SystemRDL 真源的顶层 addrmap** —— [3.build/csr_build/csr.rdl:L930-L952](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L930-L952)：

```
addrmap wireguard {
   external mem imem { ... mementries = 8192; memwidth = 32; } imem @ 0x0000_0000;
   external mem dmem { ... mementries = 8192; memwidth = 32; } dmem @ 0x1000_0000;
   csr csr @ 0x2000_0000;
};
```

这是硬件侧的「权威地址」：`imem @ 0x0000_0000`、`dmem @ 0x1000_0000`、`csr @ 0x2000_0000`。`mementries=8192`、`memwidth=32` 即 32 KB 物理深度。

**⑥ HAL 把 CSR 基址焊死** —— 自动生成的 [3.build/csr_build/generated-files/csr_hw.h:L1373-L1391](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h#L1373-L1391)：

```cpp
class csr_vp_t {
   csr_vp_t(uint32_t* base_addr = (uint32_t*)0x20000000) { ... }
   //   hw_id         = base_addr + 0x7c/4;
   //   hw_version    = base_addr + 0x80/4;
   //   routing_table = base_addr + 0x400/4;
   //   cryptokey_table = base_addr + 0x2000/4;
};
```

`main()` 里 `new csr_vp_t()` 就用这个默认基址 `0x20000000`，见 [2.sw/app/main.cpp:L784-L785](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L784-L785)。这样 `csr->dpe->fcr->pause(1)` 实际写的是 `0x20000000 + dpe 偏移 + fcr 偏移`，正好命中 `soc_fabric` 译码的 CSR 窗口。

**⑦ 三方对照表（本节核心结论）**

| 区域 | `link_map.lds`（TCM+HARVARD） | `csr.rdl` wireguard addrmap | HAL 硬编码 |
|---|---|---|---|
| IMEM | `ORIGIN=0x00000000`，窗口 64K | `imem @ 0x0000_0000`，8192×32b=32KB | `imem_vp_t` base `0x0` |
| DMEM | `ORIGIN=0x10000000`，窗口 64K | `dmem @ 0x1000_0000`，8192×32b=32KB | `dmem_vp_t` base `0x10000000` |
| CSR  | 无独立段（注释掉 IO） | `csr @ 0x2000_0000` | `csr_vp_t` base `0x20000000` |

**基址三方完全一致**。唯一的细微差异是尺寸：链接脚本保留 64K 地址窗口，而 RDL 声明的物理 BRAM 深度是 32 KB（8192 项×4 字节）；窗口大于物理是允许的——只要软件不超出 32 KB 实际容量即可。

#### 4.3.4 代码实践（本讲主实践）

这是本讲指定的核心实践：**从 `link_map.lds` 找到 IMEM/DMEM/CSR 区间，与 `csr.rdl` 的 `wireguard addrmap` 对照确认一致**。

1. **实践目标**：亲手验证「软件地址空间」与「硬件地址空间」三方一致，并发现 CSR 段在链接脚本里「缺席」这一关键设计点。
2. **操作步骤**：
   - 打开 [2.sw/link_map.lds:L63-L112](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L63-L112)，记录 `TCM=1, HARVARD=1` 分支下 IMEM、DMEM 的 `ORIGIN`；确认 CSR/I/O 段被注释掉。
   - 打开 [3.build/csr_build/csr.rdl:L937-L951](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L937-L951)，记录 `imem`、`dmem`、`csr` 的地址。
   - 打开 [3.build/csr_build/generated-files/csr_hw.h:L1376](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/generated-files/csr_hw.h#L1376)，确认 `csr_vp_t` 默认 `base_addr`。
   - 把三组数填进上面的对照表。
3. **需要观察的现象**：三处基址是否两两相等；CSR 在链接脚本里是否真的没有段；尺寸是否有差异。
4. **预期结果**：IMEM `0x0000_0000`、DMEM `0x1000_0000`、CSR `0x2000_0000` 三方一致；CSR 访问靠 HAL 指针而非链接段；链接窗口 64K vs 物理 32KB 的差异被记录。
5. 待本地验证（纯文件阅读与对照，无需编译运行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CSR 没有像 IMEM/DMEM 那样在 `link_map.lds` 里有一个独立的 `MEMORY` 段？
**答案**：因为 CSR 不是「存放变量的存储器」，而是内存映射 I/O。固件不会把全局变量放进 CSR 区（那是硬件寄存器，写了会触发硬件动作），所以链接器不需要为它分配段。访问 CSR 靠 HAL 的硬编码指针 `0x20000000`，由 `soc_fabric` 做地址译码命中。

**练习 2**：链接脚本写 IMEM/DMEM 各 64K，但 `csr.rdl` 写 8192 项×4 字节=32 KB。这两个数「打架」吗？以哪个为准？
**答案**：不打架。`csr.rdl` 的 `mementries` 描述的是**物理 BRAM 真实深度**（32 KB），链接脚本的 `LENGTH` 描述的是**允许链接器使用的地址窗口**（64K）。窗口可以大于物理容量，只要软件实际占用不超过 32 KB 即可。物理实现以 RDL 为准。

**练习 3**：把 `-DHARVARD=1` 改成冯·诺依曼（去掉 HARVARD）后，IMEM/DMEM 的地址会怎样变？（提示：看链接脚本 `#else` 分支）
**答案**：在非哈佛分支里，IMEM/DMEM 合并成单一的 `MEM`，对外用 8 MB 片外 SDRAM，`ORIGIN = 0x40000000`（见 [2.sw/link_map.lds:L96-L100](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/link_map.lds#L96-L100)）。此时 `.text` 与 `.data` 共用一段内存，也就不再需要「`.data` 跨存储器搬运」那段 boot_crt 逻辑。

## 5. 综合实践

**任务：追踪一个数据从「上电」到「点亮 CLI 提示符」的完整地址路径，把本讲三个模块串起来。**

请按下列步骤，在源码里走一遍：

1. **复位起点（4.2）**：确认 CPU 从 `0x0000_0000`（IMEM）取第一条指令，即 [boot_crt.s:L66](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/boot_crt.s#L66) 的 `_boot_crt`。
2. **搬数据（4.2+4.3）**：跟着 boot_crt 把 `.data` 从 IMEM 的 `_idata_start` 搬到 DMEM 的 `_data_start`（`0x1000_0000` 一带）；说明这一步为什么在哈佛架构下必需。
3. **进 main（4.1）**：`call main` 进入 [main.cpp:L784](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L784) 的 `int main(void)`，`new csr_vp_t()` 拿到指向 `0x2000_0000` 的 HAL 根指针。
4. **校验硬件（4.3）**：`main` 读 `csr->hw_id->VENDOR()` 与 `PRODUCT()`（[main.cpp:L791-L795](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L791-L795)），这次读访问地址 `0x20000000 + 0x7c`，经 `soc_fabric` 译码命中 CSR——如果读到的不是 `0xCCBA/0xCACA`（[csr.rdl:L467-L474](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L467-L474)），就 `ebreak` 停机。
5. **输出提示符（4.1）**：校验通过后经 UART 打印 `(wireguard-fpga)# `（[main.cpp:L813-L815](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L813-L815)）。

**产出**：画一张时序/地址流图，标注每一步用到的地址（`0x0`、`0x1000_0000`、`0x2000_0000`）和它属于哪个区域（IMEM/DMEM/CSR）。这张图能直观说明：本讲的三个模块是如何在同一次「上电到 CLI」过程中协同的。

## 6. 本讲小结

- 控制面是一个无 OS、无 libc 的裸机 C/C++ 程序，以 **WireGuard Agent** 为核心，外加 curve25519/chacha20poly1305/blake2s/hkdf/random/timer 等密码与 I/O 原语，全部裁剪成「无动态分配、可移植、适合裸机」。
- `boot_crt.s` 是最小 CRT：当前构建下做三件事——**搬运 `.data`（哈佛架构下从 IMEM 的 LMA 搬到 DMEM 的 VMA）、清零 `.bss`、设 sp/gp 后 `call main`**，`main` 返回后死循环兜底。
- 内存空间分三段：**IMEM `0x0000_0000`**（指令，BRAM）、**DMEM `0x1000_0000`**（数据，BRAM）、**CSR `0x2000_0000`**（MMIO 外设）。
- 这三段地址由三方共同维护：`link_map.lds`（软件链接）、`csr.rdl` 的 `wireguard addrmap`（硬件真源）、HAL `csr_vp_t` 硬编码基址——**基址三方完全一致**。
- 一个反直觉但关键的点：**CSR 在链接脚本里没有独立段**，它是内存映射 I/O，靠 HAL 的 `0x20000000` 指针 + `soc_fabric` 地址译码访问，而不是放变量的存储区。
- 链接脚本的 `.data : AT(_text_end)` 与 boot_crt 的搬运循环，是哈佛架构「代码在 IMEM、数据在 DMEM，但映像只能初始化 IMEM」这一矛盾的标准解法。

## 7. 下一步学习建议

- **u6-l2 软件加密原语库**：深入本讲 4.1 提到的 curve25519/blake2s/hkdf/random/timer 各原语的实现细节，理解常时间标量乘、`rdcycle` 熵源等关键设计。
- **u6-l3 网络栈与 CLI**：看 `ethernet.c`/`network.c`/`string_bare.c` 如何在无 libc 下实现最小 IP/UDP/ARP/ICMP 栈，以及 CLI 命令如何最终落到硬件表。
- **u6-l4 软件控制流：收发包与表更新**：把本讲的组件总览与 `main()` 主循环串成完整调用链，看握手完成后 CPU 如何经 HAL + FCR 原子更新 routing/cryptokey 表（承接 u3-l3、u3-l4）。
- 复习对照：u2-l4（`soc_fabric` 地址译码，解释了 CSR 窗口 `addr[31:29]==1` 为什么是 `0x2000_0000`）、u3-l1（`csr.rdl` 字段语法）、u3-l2（HAL 的层级指针访问 API 与 `0x20000000` 基址的由来）。
