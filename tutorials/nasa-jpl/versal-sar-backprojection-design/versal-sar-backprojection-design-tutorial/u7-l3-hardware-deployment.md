# 硬件部署：Yocto、NFS/TFTP 与 JTAG 烧写

## 1. 本讲目标

学完本讲，你应当能够：

- 说清从「在 x86 主机上执行完 `make`」到「在 VCK190 板卡的 ARM 上跑起 `sar_backproject.elf`」之间，一共要搬运哪些产物、分别走哪条通路（JTAG / TFTP / NFS）。
- 理解为什么主机程序是 **aarch64 交叉编译** 的、Yocto 导出的 SDK 与 sysroot 在其中扮演什么角色。
- 掌握 **NFS（rootfs）+ TFTP（内核与 pxelinux）+ JTAG（BOOT.BIN）** 这条网络启动链的拼装顺序，并读懂 `default-arm-versal` 里 `nfsroot`/`ip` 等 bootargs 每个字段的含义。
- 独立运行 `xsct flash_bootbin_xsct.tcl`，明白它那几行 Tcl 各自在做什么。

## 2. 前置知识

在进入部署细节前，先用三段话建立直觉。你已经在前序讲义里见过 Versal 的三引擎（ARM/PL/AIE）和 `make` 把它们编译链接打包的过程，本讲只关心「东西造好了之后怎么送上板、怎么让它跑起来」。请先确认你理解下面这些前置概念：

- **交叉编译（cross-compilation）**：主机开发机一般是 x86_64，而板卡上跑的 ARM Cortex-A72 是 aarch64 架构，两者 CPU 指令集不同。所以主机程序不能直接用本机的 gcc 编译，必须用一套「在 x86 上运行、却产出 aarch64 代码」的工具链，这叫交叉工具链。
- **sysroot（系统根目录）**：交叉编译器在链接时需要找到目标板上的 C 库（libc）、XRT 运行库等头文件和 `.so`。把这些目标板文件整棵「假装成根目录」提供给编译器的目录，就叫 sysroot。它必须和板卡上 rootfs 里的库版本一致，否则编出的程序上板会因 ABI 不匹配而跑不起来。
- **BOOT.BIN**：Versal 上电后最先由片上 ROM 加载的「引导镜像」，里面通常打包了 FSBL（First Stage Boot Loader）、ATF（ARM Trusted Firmware，即 BL31）、U-Boot，以及把 PL/AIE 配置下去的 PDI（Programmable Device Image）。它决定了「板子接下来去哪里找内核、怎么启动」。
- **TFTP / NFS / DHCP(BOOTP)**：三者都是网络协议。TFTP 是极简的文件传输协议，这里用来把内核镜像 `Image` 和 pxelinux 配置推给板卡；NFS 让板卡把主机上的一个目录当作自己的根文件系统挂载；BOOTP/DHCP 则给板卡分配 IP，让它能找到 TFTP/NFS 服务器。
- **JTAG**：一种硬件调试接口，能越过任何软件直接「抓住」芯片。这里用它把 BOOT.BIN 灌进 Versal，相当于在系统还没有 OS 之前就从物理层把引导镜像塞进去。

本讲依赖 [u1-l3 构建系统与 Makefile 目标](u1-l3-build-system-and-makefile.md) 中讲过的 `make package` 产物链路，建议先回顾那篇。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md) | 部署流程的「主说明书」，Yocto/SDK/NFS/TFTP/JTAG 各节都在这里 |
| [helper_scripts/env_setup.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh) | 导出 Vitis 工具链、Yocto SDK 与 DTB/BL31/U-Boot/Image/rootfs 等所有路径的环境变量脚本 |
| [Makefile](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile) | `package` 目标用 `v++ -p` 把 XSA/libadf.a/host elf 打包成 SD 卡内容（含 BOOT.BIN） |
| [helper_scripts/1_copy_tftpboot_files.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/1_copy_tftpboot_files.sh) | 把内核 Image、system.dtb、pxelinux.cfg 拷进 `/tftpboot` |
| [helper_scripts/2_copy_nfs_files.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/2_copy_nfs_files.sh) | 把 Yocto 产出的 rootfs 压缩包解压进 NFS 目录 |
| [helper_scripts/3_copy_app_files.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/3_copy_app_files.sh) | 把 a.xclbin/elf/运行脚本/数据集拷进 NFS 的 `/home/root/app` |
| [helper_scripts/pxelinux.cfg/default-arm-versal](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/pxelinux.cfg/default-arm-versal) | pxelinux 启动配置，含 `nfsroot`/`ip` 等 bootargs |
| [helper_scripts/flash_bootbin_xsct.tcl](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/flash_bootbin_xsct.tcl) | 经 JTAG 把 BOOT.BIN 灌进 Versal 的 Tcl 脚本 |
| [helper_scripts/create_dts.tcl](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/create_dts.tcl) | 从 XSA 生成设备树模板（定制硬件时用） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**Yocto 构建、SDK 导出与 sysroot 交叉编译**、**NFS + TFTP + pxelinux 网络启动链**、**xsct 经 JTAG 烧写 BOOT.BIN**。三者恰好对应「把程序编对、把 OS 送上去、把引导镜像灌进去」三件事。

### 4.1 Yocto 构建、SDK 导出与 sysroot 交叉编译

#### 4.1.1 概念说明

部署的第一道关不是「怎么送」，而是「主机程序要在哪套库上编译」。SAR 主机程序 `sar_backproject.elf` 跑在板卡的 ARM 上，它要调用 XRT（Xilinx Runtime）来控制 AIE 图和 PL 内核，而 XRT 库本身是随板卡 Linux rootfs 一起分发的。如果用主机 x86 的系统库去链接，编出来的 elf 上板根本找不到匹配的 `.so`。

Yocto 解决的就是这件事：它是一个用来「从源码构建整套嵌入式 Linux 发行版」的框架，在这里它产出三样东西——

1. **rootfs**：板卡上跑的整个 Linux 根文件系统（含 libc、XRT、shell 等）。
2. **内核 Image / DTB / U-Boot / BL31**：启动链上的各个组件。
3. **SDK（含 sysroot 的交叉工具链）**：让主机能够针对这套 rootfs 做 aarch64 交叉编译。

需要特别强调：**本仓库本身不含 Yocto**。Yocto 源码在另一个仓库 `versal-yocto-build`，要靠 [versal-manifest](https://github.com/nasa-jpl/versal-manifest) 把多个仓库一起拉下来（README 的 FAQ 第 1 条专门解释了为什么要这样拆分）。所以本仓库里你只会看到「引用 Yocto 产物路径」的脚本，看不到 Yocto 构建逻辑本身。

#### 4.1.2 核心流程

Yocto 侧与 SDK 导出的流程（路径均相对 workspace 根目录）：

```text
versal-yocto-build/                 # 另一个仓库
  docker build → source build.sh    # 在 Docker 里跑 Yocto，耗时 ≥1 小时
  workspace/poky/build/tmp/deploy/
    images/vck190-versal/           # 产物1：启动链组件
      Image                         #   Linux 内核
      system.dtb                    #   设备树
      u-boot.elf                    #   U-Boot
      arm-trusted-firmware.elf      #   BL31 (ATF)
      jpl-versal-image-...rootfs.tar.gz   # 产物2：rootfs 压缩包
    sdk/                            # 产物3：SDK 安装器
      poky-glibc-...-toolchain-5.0.3.sh
        ↓ ./...sh -d toolchain -y   # 执行后解出交叉工具链 + sysroot 到 toolchain/
```

导出 SDK 后，主机就能用它交叉编译；`env_setup.sh` 再把工具链、Vitis、以及上面这些 Yocto 产物的绝对路径都导成环境变量，供 `Makefile` 在 `host` 目标里交叉编译 elf、在 `package` 目标里把启动链组件塞进 BOOT.BIN。

#### 4.1.3 源码精读

**第一步：在 Docker 里跑 Yocto 并构建。** README 把这一步写成一组命令（注意它在一个独立的 `versal-yocto-build` 仓库里执行）：

[README.md:60-78](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L60-L78) — 用 Docker 构建镜像、进容器、`source build.sh` 启动 Yocto；README 明确提示「Yocto build can take an hour or longer」。

**第二步：导出 SDK 到 `toolchain/` 目录。** 退出 Docker 后，在主机上执行 SDK 安装器：

[README.md:85-96](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L85-L96) — 关键命令是

```bash
./poky-glibc-x86_64-jpl-versal-image-cortexa72-cortexa53-vck190-versal-toolchain-5.0.3.sh -d toolchain -y
```

文件名里的 `cortexa72-cortexa53` 说明这套工具链面向 Versal 的 ARM 核（A72 为主、A53 为辅），`-d toolchain` 指定安装目录，`-y` 自动确认。README 注明这会让「设计应用代码 link against the sysroot that Yocto created」——这就是 sysroot 的来源。

**第三步：`env_setup.sh` 把一切路径接好。** 这个脚本要被 `source`（不是执行），因为它要往当前 shell 注入 `export` 环境变量。它干了五件事：

[helper_scripts/env_setup.sh:12-15](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh#L12-L15) — 导出 Vitis 安装路径 `XILINX_VITIS`、平台库 `PLATFORM_REPO_PATHS`、功耗模型 `PDM_PATH`，以及把 Yocto 导出的 SDK 指给 `SDK_PATH`。

[helper_scripts/env_setup.sh:20-24](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh#L20-L24) — 这四行是启动链组件的绝对路径，全部指向 `versal-yocto-build/.../images/vck190-versal/` 下的 `system.dtb`、`arm-trusted-firmware.elf`（BL31）、`u-boot.elf`、`Image`（内核）、以及 rootfs 压缩包。它们随后会被 `Makefile` 的 `package` 目标用 `--package.*` 选项一一塞进 BOOT.BIN。

[helper_scripts/env_setup.sh:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh#L35) — `source $SDK_PATH/environment-setup-cortexa72-cortexa53-poky-linux` 这一行最关键：它会设置 `CXX`/`CC` 等环境变量指向 aarch64 交叉编译器，并让编译器默认以 Yocto 的 sysroot 为系统根目录。`Makefile` 的 `host` 目标正是依赖这个 `$CXX` 才能把主机源码交叉编译成能在板卡上运行的 elf。

[helper_scripts/env_setup.sh:47-49](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh#L47-L49) — 选定 VCK190 基础平台 `xilinx_vck190_base_202410_1` 并拼成 `PLATFORM` 变量；Makefile 一开头就校验 `PLATFORM` 是否存在，否则直接报错。

**第四步：`make package` 把启动链组件打包成 BOOT.BIN。** 这是把 Yocto 产物和本仓库产物缝合的最后一步：

[Makefile:84-109](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L84-L109) — `package` 目标调用 `v++ -p`，用一连串 `--package.*` 选项把 `env_setup.sh` 导出的 `${BL31_ELF}`/`${UBOOT}`/`${IMAGE}`/`${ROOTFS}` 以及（仅 hw 的）`${DTB}` 打包。其中：

- [Makefile:86-90](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L86-L90) — DTB 只在 `TARGET=hw` 时打包；注释解释定制 DTB 会让 QEMU（即 hw_emu/sw_emu）内核崩溃，所以仿真时不带 DTB。
- [Makefile:103](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L103) — `--package.defer_aie_run` 让 AIE 图不在上电时自动启动，而是交给主机程序 `sar_backproject.elf` 在运行时显式控制（呼应 u3-l5 里 `graph.run(0)` 由主机驱动的设计）。

这一步产出的 `build/hw/package/BOOT.BIN` 与 `build/hw/package/sd_card/` 子目录，正是后面 JTAG 与拷贝脚本要消费的输入。

#### 4.1.4 代码实践

**实践目标**：把 Yocto → SDK → 交叉编译这条「看不见的链」在源码里走一遍，理解 sysroot 为何是部署成功的前提。

**操作步骤**：

1. 打开 [helper_scripts/env_setup.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh)，找到第 35 行 `source .../environment-setup-cortexa72-cortexa53-poky-linux`。这就是 sysroot 生效的地方。
2. 在 Makefile 里搜索 `CXX` 或 `g++` 的使用位置，确认 host 目标是用环境变量里的交叉编译器（而非主机 gcc）链接 elf。
3. 对比 [README.md:85-96](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L85-L96) 的 SDK 安装器文件名里的 `cortexa72-cortexa53`，与 env_setup.sh 第 35 行 `environment-setup-cortexa72-cortexa53-...` 的字符串是否一致——它们必须对上。

**需要观察的现象**：env_setup.sh 第 12-15 行那些 `/mnt/disk/Xilinx/...` 路径是作者机器上的硬编码绝对路径，**你必须按自己环境改**（README 反复强调 "Adjust script as needed for your environment"）。

**预期结果**：`source env_setup.sh` 之后，`echo $CXX` 会输出一个形如 `aarch64-poky-linux-g++` 的交叉编译器路径，且 `which vivado`/`which vitis` 都能找到。如果 `PLATFORM` 为空，`make` 会立即在第 56 行报错退出。

> 说明：本实践是「源码阅读 + 环境检查」型，不要求你真有一套 Vitis/Yocto 安装；重点是看懂路径依赖关系。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能用主机自带的 `g++` 直接编译 `design/host/main.cpp`？

**参考答案**：主机 `g++` 产出 x86_64 代码并链接主机 x86 的 libc/libstdc++，而板卡 ARM 是 aarch64 架构、rootfs 里的库也是 aarch64 版本。架构和 ABI 都不匹配，elf 上板根本无法执行。必须用 Yocto 导出的 aarch64 交叉编译器，并以其 sysroot 为系统根，才能产出能在板卡 rootfs 上正确链接运行的 elf。

**练习 2**：`env_setup.sh` 为什么要用 `source` 而不是 `bash env_setup.sh` 执行？

**参考答案**：因为脚本里全是 `export` 语句，目的是把环境变量注入**当前 shell**，供随后的 `make` 读取。用 `bash env_setup.sh` 会在子 shell 里执行，`export` 的变量随子 shell 退出就消失，当前 shell 的 `make` 拿不到 `PLATFORM` 等变量。

---

### 4.2 NFS + TFTP + pxelinux 网络启动链

#### 4.2.1 概念说明

板子上电、BOOT.BIN 被加载之后，U-Boot 要去「找内核、找 rootfs」。最朴素的方式是把所有东西都摆到 SD 卡上，但 README 明确说开发期**推荐用网络（JTAG + TFTP + NFS）**，因为这样改 rootfs / 内核 / 应用都不用反复拔插 SD 卡，迭代最快。这条链路里三者分工明确：

- **JTAG**：只负责把 BOOT.BIN 灌进去（下一节细讲）。BOOT.BIN 里已经包含 U-Boot。
- **TFTP**：U-Boot 启动后，通过 TFTP 从主机下载 Linux 内核 `Image`、设备树 `system.dtb`，以及一个 **pxelinux 配置文件**（即 `default-arm-versal`）。pxelinux 是一种约定：U-Boot 用 `pxe get` / `pxe boot` 命令读取这个配置文件，按里面的 `KERNEL`/`FDT`/`APPEND` 指令加载内核并传递 bootargs。
- **NFS**：内核启动后，按 bootargs 里的 `nfsroot=` 把主机上的一个目录挂载为自己的根文件系统。此后板卡上「`/`」实际就是主机硬盘上的那个目录。

一句话：**JTAG 送引导，TFTP 送内核，NFS 送整个文件系统（含应用）**。

#### 4.2.2 核心流程

部署期（`make` 完成后）的人工流程，对应 README「Flashing Project to Versal Hardware」一章：

```text
[主机侧准备]
  1. 配置 /etc/exports，exportfs -a，重启 nfs-kernel-server      # 开 NFS
  2. 跑 1_copy_tftpboot_files.sh → /tftpboot 得到 Image/system.dtb/pxelinux.cfg
  3. 跑 2_copy_nfs_files.sh      → /nfs/versal/rootfs 得到完整 rootfs
  4. 跑 3_copy_app_files.sh      → /nfs/versal/rootfs/home/root/app 得到应用与数据
  5. 改 pxelinux.cfg/default-arm-versal 里的 nfsroot/ip IP 段，匹配你的网段

[板卡侧上电]
  6. xsct flash_bootbin_xsct.tcl  → JTAG 灌 BOOT.BIN（含 U-Boot）
  7. U-Boot 起来 → pxe get 取 default-arm-versal → pxe boot
  8. U-Boot 按 KERNEL/FDT 经 TFTP 拉内核 Image + system.dtb
  9. 内核启动，按 APPEND 里 nfsroot= 经 NFS 挂载 rootfs
 10. 登录板卡 → cd /home/root/app → ./run_script_hw.sh ... 跑起来
```

#### 4.2.3 源码精读

**NFS 服务端配置。** README 给出 `/etc/exports` 的示例行：

[README.md:122-143](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L122-L143) — 示例 `/nfs/versal/rootfs 192.168.10.0/24(rw,no_root_squash,crossmnt)`，并说明要先 `sudo apt install nfs-kernel-server`，配完后 `exportfs -a` + 重启服务。`no_root_squash` 对嵌入式开发很关键：它允许板卡以 root 身份写 NFS（否则 root 操作会被映射成匿名用户，写不进去）。

**pxelinux 启动配置与 bootargs。** 整个文件只有 4 行，却决定了内核怎么启动：

[helper_scripts/pxelinux.cfg/default-arm-versal:1-4](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/pxelinux.cfg/default-arm-versal#L1-L4) — 这是 pxelinux 协议要求的格式：

- `KERNEL Image`：告诉 U-Boot 去加载名为 `Image` 的 Linux 内核。
- `FDT system.dtb`：同时加载设备树 `system.dtb`（FDT = Flattened Device Tree）。
- `APPEND ...`：这一长串就是传给内核的 **bootargs**。把它拆开看：

```text
console=ttyUSB1 earlycon=pl011,mmio32,0xFF000000,115200n8   # 串口控制台
clk_ignore_unused                                            # 别关未使用时钟(防 AIE 时钟被关)
cma=600MB                                                    # 给 GPU/XRT 预留 600MB 连续内存
root=/dev/nfs rootfstype=nfs                                 # 根文件系统走 NFS
nfsroot=192.168.10.1:/nfs/versal/rootfs,tcp,nfsvers=3        # NFS 服务器地址与目录
ip=192.168.10.2:::255.255.255.0:versal:eth0:bootp            # 板卡自己的网络配置
rw                                                          # rootfs 可读写
```

README 对 `nfsroot` 和 `ip` 两个最易错的字段做了逐字段注释：

[README.md:158-169](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L158-L169) — 解释 `nfsroot=192.168.10.1:/nfs/versal/rootfs,tcp,nfsvers=3`：`192.168.10.1` 是 NFS 服务器（你的主机）IP，`/nfs/versal/rootfs` 是 NFS 目录，`tcp`/`nfsvers=3` 指定传输协议与版本；`ip=192.168.10.2:::255.255.255.0:versal:eth0:bootp` 中 `192.168.10.2` 是板卡 IP、`255.255.255.0` 是掩码、`versal` 是主机名、`eth0` 是早期拉起的网卡、`bootp` 是自动配置方式。

> 提示：`ip=` 字段用的是 Linux 内核 `ip=` 的多冒号语法 `ip=<client-ip>:<server-ip>:<gw-ip>:<netmask>:<host>:<device>:<autoconf>`，本设计把中间三个留空（`:::`），由 `nfsroot` 单独指明服务器，所以板卡 IP、掩码、主机名、网卡、自动配置方式这几个位置要数对冒号。

**三个拷贝脚本各自的输入/输出。** 这三个脚本结构高度一致：都用 `WORKSPACE_PATH=$(dirname $(dirname ...))` 算出 workspace 根，然后从 Yocto 产物目录或本仓库 build 目录拷到 `/tftpboot` 或 `/nfs/...`。

[helper_scripts/1_copy_tftpboot_files.sh:9-17](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/1_copy_tftpboot_files.sh#L9-L17) — 源是 Yocto 的 `images/vck190-versal/`，目的是 `/tftpboot`。第 17 行那一条 `cp -a` 一次性把 `Image*`、`devicetree` 目录、本仓库的 `pxelinux.cfg/` 目录、以及 `system.dtb` 全拷过去——也就是说 pxelinux 配置文件也是经 TFTP 送给 U-Boot 的。

[helper_scripts/2_copy_nfs_files.sh:9-15](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/2_copy_nfs_files.sh#L9-L15) — 源是 Yocto 产出的 `jpl-versal-image-...rootfs.tar.gz`，目的是 `/nfs/versal/rootfs`。关键动作是 `sudo tar -xvf ...rootfs.tar.gz -C ${NFS_PATH}`：把整个 rootfs 压缩包解压进 NFS 目录，于是板卡的「根」就是主机上这个解压后的目录树。

[helper_scripts/3_copy_app_files.sh:9-35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/3_copy_app_files.sh#L9-L35) — 源是本仓库的 `build/hw/package/sd_card/`（即 `make package` 的产物），目的是 NFS 里的 `/home/root/app`。它拷了五类文件，值得逐行看：

- [3_copy_app_files.sh:22](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/3_copy_app_files.sh#L22) — `a.xclbin`（编译好的 PL+AIE 设计）、`sar_backproject.elf`（主机程序）、`run_script_hw.sh`（运行脚本）。
- [3_copy_app_files.sh:25](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/3_copy_app_files.sh#L25) — slowtime 数据集 CSV。
- [3_copy_app_files.sh:28](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/3_copy_app_files.sh#L28) — `log_ina226.sh`（INA226 功耗采样脚本，对应 u8-l2 的实测功耗度量）。
- [3_copy_app_files.sh:31-32](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/3_copy_app_files.sh#L31-L32) — 这里有个精巧细节：先用 `grep '^#define RC_SAMPLES' .../common.h | awk '{print $3}'` 从 `common.h` 读出 `RC_SAMPLES` 的值，再据此挑选对应那份 phdata CSV（如 `gotcha_phdata_512-out-of-424-...`）。这与 u1-l4 讲的「Makefile 按 `common.h` 选 phdata 文件」是同一套机制，保证拷上去的数据集与编译时用的 `RC_SAMPLES` 一致。
- [3_copy_app_files.sh:35](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/3_copy_app_files.sh#L35) — `chmod +x` 给 elf 和运行脚本加可执行位（tar 解压 / NFS 拷贝可能丢失执行位）。

**没有 DHCP 时手动配 U-Boot 网络。** README 还给了「无 DHCP 服务器」场景下，要在 U-Boot 串口里手动敲的网络设置：

[README.md:243-263](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L243-L263) — 在 U-Boot 自动启动前按回车打断，`setenv ipaddr/netmask/serverip` 设好板卡与服务器 IP，再用 `setenv bootcmd 'if pxe get; then pxe boot; fi'` 让 U-Boot 去取 pxelinux 配置并启动。这一段印证了「TFTP 客户端负责把 default-arm-versal 传给 U-Boot」的机制。

#### 4.2.4 代码实践

**实践目标**：把本讲规格里要求的核心任务做掉——列出从 `make` 完成到板卡跑起 elf 所需的「文件搬运清单」，并解释 bootargs 中 `nfsroot`/`ip` 各字段。

**操作步骤**：

1. 对照三个拷贝脚本，按下表填出「来源 → 目的 → 文件」三列（答案见下方「预期结果」）：

| 脚本 | 来源目录 | 目的目录 | 搬运的文件 |
|------|----------|----------|------------|
| `1_copy_tftpboot_files.sh` | Yocto `images/vck190-versal/` + 本仓库 `pxelinux.cfg/` | `/tftpboot` | ? |
| `2_copy_nfs_files.sh` | Yocto `...rootfs.tar.gz` | `/nfs/versal/rootfs` | ? |
| `3_copy_app_files.sh` | 本仓库 `build/hw/package/sd_card/` | `/nfs/versal/rootfs/home/root/app` | ? |

2. 打开 [helper_scripts/pxelinux.cfg/default-arm-versal:4](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/pxelinux.cfg/default-arm-versal#L4)，把 `nfsroot=` 和 `ip=` 两段逐字段拆开，写出每个字段的含义与默认值。

**需要观察的现象**：注意 `nfsroot` 里的服务器 IP `192.168.10.1` 与 `ip=` 里板卡 IP `192.168.10.2` 必须在同一网段（都是 `192.168.10.0/24`）；若你的主机网卡在别的网段，这俩 IP 都要改，且要和 `/etc/exports`、U-Boot 里的 `serverip`/`ipaddr` 三处保持一致。

**预期结果**（文件清单）：

- `1_copy_tftpboot_files.sh`：`Image*`、`devicetree/`、`pxelinux.cfg/`（含 `default-arm-versal`）、`system.dtb` → `/tftpboot`。作用：给 U-Boot 经 TFTP 提供内核 + 设备树 + 启动配置。
- `2_copy_nfs_files.sh`：整个 `rootfs.tar.gz` 解压 → `/nfs/versal/rootfs`。作用：构成板卡的整个根文件系统。
- `3_copy_app_files.sh`：`a.xclbin`、`sar_backproject.elf`、`run_script_hw.sh`、slowtime CSV、`log_ina226.sh`、按 `RC_SAMPLES` 选择的 phdata CSV → `/home/root/app`。作用：板卡登录后可直接 `cd /home/root/app && ./run_script_hw.sh` 跑设计。

bootargs 字段拆解见上面 4.2.3 的「pxelinux 启动配置」一段，要点：`nfsroot=<服务器IP>:<目录>,<传输协议>,<NFS版本>`；`ip=<板卡IP>:::<掩码>:<主机名>:<网卡>:<自动配置方式>`。

> 说明：本实践无需真实硬件，重点是把「文件流转」和「bootargs 语义」读懂；真机搬运时所有路径/IP 都要按你环境改。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `nfsroot` 里的目录改成 `/nfs/versal/rootfs`，但主机 `/etc/exports` 里 export 的是 `/nfs/versal/myroot`，会发生什么？

**参考答案**：内核启动时 NFS 挂载会失败——因为 NFS 服务器只对 `/etc/exports` 里声明的目录授权访问，`nfsroot` 指向的目录未被 export，挂载请求会被拒绝，内核卡在「无法挂载 rootfs」、无法继续启动（VFS panic）。

**练习 2**：`cma=600MB` 这个 bootargs 在本设计里为什么重要？（提示：联系 u3-l2 的 buffer 分配）

**参考答案**：CMA（Contiguous Memory Allocator）预留一大块物理连续内存。XRT 的 `xrt::bo` 缓冲对象、AIE 的 GMIO 通道、PL 的 m_axi DMA 都需要物理连续的 DDR 区段来做 DMA。预留 600MB CMA 是为了保证反投影那些大 buffer（RC/像素/图像）能成功分配到连续物理内存，否则运行时会因内存碎片化而分配失败。

---

### 4.3 xsct 经 JTAG 烧写 BOOT.BIN

#### 4.3.1 概念说明

整条网络链里，唯一不靠网络、也不靠 SD 卡的，就是把 BOOT.BIN 灌进芯片这一步——它走 **JTAG**。原因是：芯片刚上电时，OS 还没起来、网络还没通，唯一能从外部「直接控制硅片」的通道就是 JTAG 这根硬件调试线。Xilinx 的 `xsct`（Xilinx Software Command-line Tool）是一个 Tcl 解释器，专门用来经 JTAG 对 Versal 做连接、复位、编程等操作。

为什么烧的是 BOOT.BIN 而不是别的？因为 BOOT.BIN 里打包了 FSBL + ATF(BL31) + U-Boot + PDI（把 PL 和 AIE 配置下去的镜像）。一旦 BOOT.BIN 灌进去并复位，芯片就会：加载 FSBL → FSBL 配置 PL/AIE（下发 PDI）→ 启动 ATF → 启动 U-Boot → U-Boot 再走 TFTP/NFS 拉起 Linux。所以 BOOT.BIN 是「万事开头」的那一个文件。

#### 4.3.2 核心流程

`flash_bootbin_xsct.tcl` 只需 5 行有效命令，流程如下：

```text
1. 算出仓库根路径 design_build_path
2. connect              # 连 JTAG 服务器/线缆
3. targets -set ...     # 选中名为 "Versal*" 的目标芯片
4. rst                  # 系统复位
5. device program .../build/hw/package/BOOT.BIN   # 把 BOOT.BIN 编程进芯片
```

#### 4.3.3 源码精读

整个脚本极短，但每行都不可省：

[helper_scripts/flash_bootbin_xsct.tcl:5](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/flash_bootbin_xsct.tcl#L5) — `set design_build_path [file dirname [file dirname [file normalize [info script]]]]`：`[info script]` 拿到当前脚本自身路径，两层 `file dirname` 向上回退到仓库根目录（从 `helper_scripts/` 退到仓库根）。这样无论你在哪个目录调用 `xsct`，都能定位到 `build/hw/package/BOOT.BIN`。

[helper_scripts/flash_bootbin_xsct.tcl:8](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/flash_bootbin_xsct.tcl#L8) — `connect`：建立与本地/远程 JTAG hw_server 的连接。

[helper_scripts/flash_bootbin_xsct.tcl:11](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/flash_bootbin_xsct.tcl#L11) — `targets -set -nocase -filter {name =~ "Versal*"}`：在已连接的目标列表里，按名字筛选并选中 Versal 芯片。`-nocase` 大小写不敏感，`-filter` 用通配匹配，保证选中的是 Versal 主芯片而非 System Controller 等。

[helper_scripts/flash_bootbin_xsct.tcl:14](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/flash_bootbin_xsct.tcl#L14) — `rst`：系统复位，把芯片置于干净状态再编程。

[helper_scripts/flash_bootbin_xsct.tcl:17](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/flash_bootbin_xsct.tcl#L17) — `device program $design_build_path/build/hw/package/BOOT.BIN`：核心一行，把 `make package` 产出的 BOOT.BIN 经 JTAG 编程进 Versal。注意路径写死 `build/hw/`，所以必须用 `TARGET=hw` 构建过（仿真产物在别的目录）。

调用方式见 README：

[README.md:236-239](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/README.md#L236-L239) — 在仓库根执行 `xsct ./helper_scripts/flash_bootbin_xsct.tcl`。烧写前要先打开串口（`/dev/ttyUSB3`，`screen ... 115200`）观察 U-Boot/Linux 启动信息。

> 补充：若需要根据本设计的硬件定制设备树（例如改了 PL 的地址映射），还有一个 [helper_scripts/create_dts.tcl:20](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/create_dts.tcl#L20)，它用 `createdts` 从链接阶段留下的 XSA 生成设备树模板，再放回 Yocto 的 `recipes-bsp/device-tree/files/versal.dts` 重新构建——这是部署期可能遇到的「定制 DTB」分支。

#### 4.3.4 代码实践

**实践目标**：在不接真机的前提下，逐行读懂这 5 行 Tcl，搞清「选中哪个目标、复位、编程」的次序为何不能乱。

**操作步骤**：

1. 打开 [flash_bootbin_xsct.tcl](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/flash_bootbin_xsct.tcl)，对照上面 4.3.3 的逐行说明，在每行旁标注「这一行的目的」。
2. 思考：为什么 `rst`（第 14 行）必须在 `device program`（第 17 行）之前？如果把这两行对调会怎样？
3. 确认第 17 行的 BOOT.BIN 路径与 `make package` 产物位置 [Makefile:43](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L43)（`PACKAGE_BUILD_DIR = build/${TARGET}/package`，hw 时即 `build/hw/package`）一致。

**需要观察的现象**：脚本路径是「写死 hw」的。若你只跑过 `TARGET=sw_emu` 或 `hw_emu`，`build/hw/package/BOOT.BIN` 不存在，`device program` 会因找不到文件而失败。

**预期结果**：复位必须在编程之前——不复位直接编程，芯片可能仍处于上次运行的脏状态（如 AIE/PL 已被配置过），导致新 BOOT.BIN 里的 PDI 下发异常；先 `rst` 清场再编程，才能保证从干净状态引导。这也是真实硬件上烧写成功的标准顺序。

> 说明：本实践为源码阅读型；真机操作时还需先 `xsdb`/`xsct` 连上 hw_server 并确认 JTAG 线缆枚举正常，这些超出本仓库脚本范围。

#### 4.3.5 小练习与答案

**练习 1**：为什么烧 BOOT.BIN 必须用 JTAG，而不能像内核/rootfs 那样走网络？

**参考答案**：BOOT.BIN 是芯片上电后由片上 ROM 最先加载的引导镜像，此时 OS、U-Boot、网络协议栈都还没起来，根本无法用 TFTP/NFS。JTAG 是独立于任何软件、直接经硬件调试接口控制硅片的通道，是「系统启动前」唯一可用的灌入手段。只有 BOOT.BIN 灌进去、U-Boot 跑起来后，后续内核/rootfs 才能走网络。

**练习 2**：`targets -set -filter {name =~ "Versal*"}` 为什么要用通配符 `Versal*` 而不是写死一个名字？

**参考答案**：VCK190 上通常有多个 JTAG 可见目标（Versal 主芯片、System Controller 等），且不同版本工具链给 Versal 主芯片起的具体名字可能带后缀（如版本号/型号）。用 `Versal*` 通配能稳健地选中 Versal 主芯片而忽略其他目标，避免因名字细微差异而选错芯片。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出一张「从 `make` 完成到板卡跑起 `sar_backproject.elf`」的完整端到端部署流水线图，并标注每一步用到哪个脚本/文件、走哪条通路（JTAG/TFTP/NFS）、产物落在板卡的什么位置。

**建议步骤**：

1. 在图的最左端列出 `make package` 的全部产物：`build/hw/package/BOOT.BIN` 与 `build/hw/package/sd_card/` 下的 `a.xclbin`、`sar_backproject.elf`、`run_script_hw.sh`、slowtime/phdata CSV；再列出 Yocto 的 `Image`/`system.dtb`/`rootfs.tar.gz`。
2. 用三条颜色不同的箭头分别表示 JTAG、TFTP、NFS，把它们对应的脚本（`flash_bootbin_xsct.tcl` / `1_copy_tftpboot_files.sh` / `2_copy_nfs_files.sh`+`3_copy_app_files.sh`）标在箭头上。
3. 在图的最右端画出板卡：BOOT.BIN 经 JTAG 进芯片 → U-Boot 经 TFTP 取内核+pxelinux → 内核经 NFS 挂 rootfs → `/home/root/app` 下有 elf/xclbin/数据 → `./run_script_hw.sh` 启动。
4. 在图旁用一段话解释「为什么主机 elf 必须用 Yocto SDK 交叉编译」以及「为什么 `cma=600MB` 和 `--package.defer_aie_run` 缺一不可」。

**自检要点**：

- 三条通路各司其职，没有混淆（JTAG 不传 rootfs、NFS 不传内核、TFTP 不传应用）。
- 能说清 `default-arm-versal` 里 `nfsroot`/`ip` 至少 6 个字段的含义。
- 能指出 `env_setup.sh` 里哪些路径必须按本机环境改、为什么必须 `source` 而非执行。

> 说明：本综合实践以「画图 + 口述」为主，目的是把三个最小模块的因果关系内化；若手头有 VCK190，可在此基础上实际执行一次部署并记录每步串口输出。

## 6. 本讲小结

- 部署链路按通路三分工：**JTAG 烧 BOOT.BIN（引导）、TFTP 送内核+pxelinux、NFS 送整个 rootfs（含应用）**；其中只有 BOOT.BIN 不走网络，因为它要在 OS 起来之前灌入。
- 主机程序 `sar_backproject.elf` 是 **aarch64 交叉编译** 产物，必须 link Yocto 导出 SDK 的 sysroot；`env_setup.sh` 第 35 行 `source .../environment-setup-cortexa72-cortexa53-poky-linux` 是 sysroot 生效的关键，且脚本必须 `source` 不能直接执行。
- `env_setup.sh` 把 Yocto 产出的 `Image`/`system.dtb`/`u-boot.elf`/`arm-trusted-firmware.elf`/rootfs 路径导成环境变量，`Makefile` 的 `package` 目标再用 `--package.*` 把它们连同 XSA、libadf.a、host elf 打包成 BOOT.BIN 与 `sd_card/`。
- `default-arm-versal` 那 4 行 pxelinux 配置决定内核怎么启动；`nfsroot=<服务器IP>:<目录>,tcp,nfsvers=3` 与 `ip=<板卡IP>:::<掩码>:<主机名>:<网卡>:<自动配置>` 是最易错的两个字段，必须与 `/etc/exports`、U-Boot 的 `serverip`/`ipaddr` 三处网段一致。
- 三个拷贝脚本各管一段：`1_copy_tftpboot_files.sh`→`/tftpboot`、`2_copy_nfs_files.sh` 解压 rootfs→`/nfs/versal/rootfs`、`3_copy_app_files.sh`→`/home/root/app`，其中第三个还按 `common.h` 的 `RC_SAMPLES` 动态挑 phdata CSV。
- `flash_bootbin_xsct.tcl` 只有 5 行有效 Tcl：算路径 → `connect` → 选 Versal 目标 → `rst` 复位 → `device program BOOT.BIN`；复位必须在编程之前，路径写死 `build/hw/`，故必须先 `TARGET=hw` 构建。

## 7. 下一步学习建议

- 若想理解 BOOT.BIN 内部到底打包了什么、`v++ -p` 各 `--package.*` 选项如何映射到 SD 卡分区结构，可重读 [u7-l1 系统集成：system.cfg、XSA 链接与打包](u7-l1-system-integration-packaging.md) 的打包一节。
- 部署完成后，下一步就是度量运行性能与功耗：继续学习 [u8-l1 AIE 与 PL 仿真流程](u8-l1-aie-pl-simulation.md) 与 [u8-l2 性能与功耗度量](u8-l2-performance-and-power-metrics.md)，其中 INA226 实测功耗用的正是本讲 `3_copy_app_files.sh` 第 28 行拷上去的 `log_ina226.sh`。
- 若你打算定制硬件（改 PL 地址映射等），可深入 [helper_scripts/create_dts.tcl](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/create_dts.tcl)，研究如何从 XSA 重新生成设备树并喂回 Yocto 重建。
