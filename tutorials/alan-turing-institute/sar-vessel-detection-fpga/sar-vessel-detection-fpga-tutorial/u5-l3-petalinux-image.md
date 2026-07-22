# 讲义 u5-l3：PetaLinux 软件镜像构建

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 PetaLinux 在端到端流水线「阶段⑤硬件/固件部署」中的角色，以及它如何消费上一讲（u5-l2）导出的 `.xsa`。
- 读懂 `helper_build_bsp.sh` 的三段式结构（`create_proj` / `customize_proj` / `build_proj`），并逐行解释它对 PetaLinux 工程做了哪些定制。
- 理解内核 DPU 驱动如何启用、以及为何脚本要禁用 `xrt`/`zocl`（vivado flow）。
- 解释 `BOOT.BIN` / `image.ub` / `boot.scr` / rootfs 在 SD 卡两个分区里的职责与启动顺序。
- 诊断并修复 `rootfs > 2GB` 引发的 `do_image_cpio` 报错，理解 `IMAGE_FSTYPES:remove` 与「切到 EXT4 分区根文件系统」两条修复为何是一致的。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**什么是 PetaLinux。** PetaLinux 是 Xilinx/AMD 为 Zynq 系列（包括 KV260 的 Zynq UltraScale+ MPSoC）提供的嵌入式 Linux 构建系统。它底层封装了 Yocto，把「内核 + 设备树 + U-Boot + 根文件系统（rootfs）」打包成一整套可烧录到 SD 卡的镜像。一句话：Vivado 造的是「硬件」（PL 比特流与地址映射），PetaLinux 造的是「跑在硬件上的 Linux」。

**什么是 BSP。** Board Support Package（板级支持包）是一个 `.bsp` 压缩包，预置了某块板子的基础内核配置、U-Boot 配置、设备树和一组预编译的引导文件。`petalinux-create -t project -s xxx.bsp` 用它来「秒建」一个针对该板的初始工程，省去从零配置引脚与时钟。

**根文件系统的两种挂法：INITRAMFS vs SD 分区。**

- **INITRAMFS（initrd）**：把 rootfs 压成 `cpio` 归档，链接进内核镜像 `image.ub`，开机时**整体加载进 DDR 内存**。
- **SD 分区根文件系统**：rootfs 作为 ext4 文件系统**驻留在 SD 卡的第二个分区**，开机时内核挂载 `/dev/mmcblk0p2`，不占 DDR。

这一区别是本讲排错章节的核心：当 rootfs 因为塞满了 Vitis AI / OpenCV / GStreamer 而超过 2GB 时，INITRAMFS 路线就会失败，必须切到 SD 分区根文件系统。

**启动链（KV260/Zynq UltraScale+）。** 上电后：PMU ROM → FSBL → U-Boot →（读 `boot.scr`）→ 加载 `image.ub`（含内核与设备树）→ Linux 内核启动 → 挂载 rootfs → 运行你后续（u7）编译的 C++ 推理程序。`BOOT.BIN` 里打包了 FSBL + U-Boot + 比特流。

> 承接：u5-l2 已用 Vivado 导出 `.xsa`（硬件平台文件）。本讲正是把 `.xsa` 喂给 PetaLinux，生成「跑得动 DPU 的 Linux」。本讲产物（SD 镜像 + 内核 DPU 驱动）又会被 u5-l4 的固件加载流程消费。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们承载了整条软件镜像构建链。

| 文件 | 作用 |
| --- | --- |
| [platform/kv260/README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md) | 硬件→软件→固件→部署的四阶段总指南；本讲关注其「Software Build (PetaLinux)」与「Troubleshooting」两节，给出命令、内核菜单路径与 `do_image_cpio` 修复方法。 |
| [platform/kv260/sw/helper_build_bsp.sh](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh) | 自动化 PetaLinux 工程创建与定制的 Bash 脚本；用 `sed` 批量改写工程配置文件，把上游通用 BSP 改造成「带 DPU 驱动 + Vitis AI 库 + EXT4 rootfs」的 KV260 镜像。 |

> 注意：脚本第 6 行引用了 `vai_petalinux_recipes/`（含 `recipes-vai-kernel`、`recipes-vitis-ai`），该目录**不在本仓库**，来自上游 `VAI-3.5-ZUP-DPU-TRD` 工程。脚本还假定同目录下存在板卡 BSP 文件 `xilinx-<board>-v2023.2-10140544.bsp`（需从 Xilinx 下载）。这两项缺失会导致脚本无法直接运行——本讲据此把「脚本自带」与「需自备」区分清楚。

## 4. 核心概念与源码讲解

### 4.1 PetaLinux BSP 构建

#### 4.1.1 概念说明

「BSP 构建」这一步要做的是：从一个**通用板卡 BSP** 起步，把它改造成**本项目专用的 PetaLinux 工程**。改造包括四件事——创建工程、关掉与本设计冲突的 XRT/Zocl 框架、把一大堆 Vitis AI 相关包塞进 rootfs、把根文件系统从 INITRAMFS 切到 SD 卡 ext4 分区。

为什么不能直接用官方 BSP 开机即用？因为官方 KV260 BSP 默认带 XRT/Zocl 软件栈（面向 XRT 调度模型），而本项目走的是 **vivado flow**——DPU 以「独立字符设备驱动」形式暴露给用户态（这与 u5-l4 `xdputil query` 输出里的 `"is_vivado_flow":true` 自洽）。两者互斥，必须先把 XRT/Zocl 关掉。

#### 4.1.2 核心流程

脚本的整体控制流是一个清晰的管道：

```text
main <board>
  ├── create_proj        # petalinux-create -t project -s <bsp>   建初始工程
  ├── customize_proj     # sed 批量改写工程配置（核心定制）
  │     ├── disable xrt / xrt-dev / zocl          （vivado flow）
  │     ├── 拷入 recipes-vai-kernel / recipes-vitis-ai
  │     ├── 追加 PKG_OPTIONAL 一大堆包到 rootfs
  │     ├── IMAGE_FSTYPES:remove cpio …           （排错修复）
  │     ├── 开 auto-login / package-management / debug-tweaks
  │     └── rootfs: INITRD → EXT4，根设备 = /dev/mmcblk0p2
  └── build_proj         # 导入 .xsa → 配内核 → 编译 → 打包 boot/wic
```

#### 4.1.3 源码精读

**入口与路径推导。** 脚本用自身所在目录推导出硬件目录、recipes 目录、BSP 与工程目录的绝对路径，并用 `set -e` 保证任何一步失败立即退出：

[platform/kv260/sw/helper_build_bsp.sh:L1-L9](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L1-L9) —— 计算 `xsadir`（指向上一级的 `hw/`，即 `.xsa` 所在）、`recipesdir`（指向缺失的 `vai_petalinux_recipes/`）、`board_bsp`、`plnxdir`（工程输出目录 `xilinx-<board>-2023.2`）。`board` 取自第一个命令行参数。

**第一步：从 BSP 创建工程。**

[platform/kv260/sw/helper_build_bsp.sh:L35-L37](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L35-L37) —— `petalinux-create -t project -s ${board_bsp}` 用预置 BSP 一键生成初始工程，`sync` 把文件落盘。这里隐含要求 `xilinx-kv260-v2023.2-10140544.bsp` 已就位（待本地确认：BSP 文件需预先从 Xilinx 下载到 `sw/` 目录）。

**第二步（关键定制之一）：关掉 XRT/Zocl，切到 vivado flow。**

[platform/kv260/sw/helper_build_bsp.sh:L43-L46](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L43-L46) —— 三条 `sed` 把 `rootfs_config` 里的 `CONFIG_xrt=y`、`CONFIG_xrt-dev=y`、`CONFIG_zocl=y` 全部改成「未设置」。这正是让本设计与 XRT 软件栈解耦、改用 DPU 内核字符驱动的开关。

**第三步（关键定制之二）：往 rootfs 塞进一大堆包。**

[platform/kv260/sw/helper_build_bsp.sh:L11-L25](https://github.com/alan-turing-institute-sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L11-L25) —— `PKG_OPTIONAL` 数组列出了要追加安装的包：`resize-part`、`dnf`、`nfs-utils`、`cmake`、`opencl-headers`/`opencl-clhpp-dev`（u8 HLS 核的 OpenCL host 会用）、`packagegroup-petalinux-x11`、`opencv`、`gstreamer`、`self-hosted`（on-target SDK）、以及三个 `vitis-ai-library` 变体（运行库 + dev 头 + dbg 符号）。

[platform/kv260/sw/helper_build_bsp.sh:L52-L59](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L52-L59) —— 把（仓库外的）`recipes-vai-kernel` 与 `recipes-vitis-ai` 拷进工程的 `meta-user` 层，再用循环把 `PKG_OPTIONAL` 逐行 `IMAGE_INSTALL:append` 到 `petalinuxbsp.conf`。**正是这一大堆包让 rootfs 体积膨胀到 >2GB**，直接埋下了下一节 `do_image_cpio` 报错的伏笔——这两节是因果关系，不是巧合。

#### 4.1.4 代码实践

**实践目标：** 在不真正跑 PetaLinux（需专用 Ubuntu 主机与几十 GB 工具链）的前提下，靠静态阅读脚本，复述 BSP 构建阶段对工程做了哪些可观测的改动。

**操作步骤：**

1. 打开 `platform/kv260/sw/helper_build_bsp.sh`，定位 `customize_proj` 函数。
2. 列出它修改的所有**目标文件**（`rootfs_config`、`petalinuxbsp.conf`、`config`），并标注每一处改动属于「关 XRT」「加包」「切 rootfs 类型」中的哪一类。
3. 把 `PKG_OPTIONAL` 里的包按用途分类：推理运行时、图像/视频处理、开发/调试、OpenCL（HLS）。

**需要观察的现象 / 预期结果：**

- 你应当能画出一张「目标文件 → 改动类别」对照表。例如 `rootfs_config` 同时承担「关 XRT」与「开 auto-login/package-management」两类改动。
- 你应当发现：脚本第 60-61 行与第 71-75 行都在围绕「让超大 rootfs 能编译通过」这一目标服务（前者删 cpio 产物，后者把根文件系统从内存式 INITRAMFS 改成 SD 卡 ext4 分区）。两处改动是**同一问题的两面**。

> 待本地验证：若你手头有 PetaLinux 2023.2 主机，运行 `bash helper_build_bsp.sh kv260`（需先备齐 BSP 与 recipes 目录），观察 `xilinx-kv260-2023.2/project-spec/` 下配置文件是否如期被改写。

#### 4.1.5 小练习与答案

**练习 1.** 脚本为什么要禁用 `zocl`？不禁用会怎样？

> **参考答案：** `zocl` 是 XRT 调度模型在 PL 侧的内核驱动（把 FPGA 当成可调度计算单元）。本项目走 vivado flow，DPU 以独立字符设备驱动（`/dev/dpu` 一类）暴露，不经过 XRT 调度。两者软件栈互斥；若不禁用 `zocl`/`xrt`，会与 DPU 内核驱动冲突，且 `xdputil query` 将无法以 vivado flow 识别 DPU。

**练习 2.** `PKG_OPTIONAL` 里同时有 `vitis-ai-library`、`vitis-ai-library-dev`、`vitis-ai-library-dbg`，三者区别是什么？为什么三个都要？

> **参考答案：** `vitis-ai-library` 是运行时动态库（推理程序链接它才能跑）；`-dev` 提供 `.h` 头文件与 `.so` 软链接，供在板上编译 C++ 程序（on-target SDK，对应 `self-hosted`）；`-dbg` 提供调试符号，供 `gdb` 排查段错误。三者分别服务「运行」「编译」「调试」三个场景。

---

### 4.2 内核 DPU 驱动配置

#### 4.2.1 概念说明

PetaLinux 工程默认的内核配置并不知道「PL 侧有一个 DPU IP」，因此需要：(1) 把 u5-l2 的 `.xsa` 硬件描述导入工程，让设备树知道 DPU 挂在哪个 AXI 地址；(2) 在内核配置里把「Xilinx DPU 驱动」编译进内核或编成模块，让 Linux 能枚举并驱动它。

README 记录的是**手动菜单**路径；`helper_build_bsp.sh` 则通过拷入 `recipes-vai-kernel`（仓库外、来自上游 TRD）来**自动**提供内核侧的 DPU 驱动集成（通常是一个内核配置片段 `.cfg` + 驱动 `bbappend`）。两条路径目标一致。

#### 4.2.2 核心流程

`build_proj` 函数把「导入硬件 → 配内核 → 编译 → 打包」串成一条流水线：

```text
cd <工程目录>
petalinux-config --get-hw-description=<hw 目录> --silentconfig   # 导入 .xsa → 生成设备树/地址
petalinux-config -c kernel                                        # 内核 menuconfig（启 DPU 驱动）
petalinux-build                                                   # 编译内核/U-Boot/rootfs/设备树
petalinux-package --boot --fsbl … --u-boot … --fpga system.bit   # 打包 BOOT.BIN
petalinux-package --wic                                           # 打包可烧录的 wic SD 镜像
```

#### 4.2.3 源码精读

**导入硬件描述（`.xsa` 交接点）。**

[platform/kv260/sw/helper_build_bsp.sh:L85-L89](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L85-L89) —— `petalinux-config --get-hw-description=${xsadir} --silentconfig` 扫描 `hw/` 目录里的 `.xsa`，从中提取 PS 外设、AXI 地址映射、DPU 中断等信息，反写进设备树与 U-Boot 配置。这一行就是 u5-l2（Vivado/XSA）→ 本讲（PetaLinux）的**唯一正式交接点**。`--silentconfig` 表示用默认值静默完成，不弹菜单。

**启用 DPU 驱动（手动菜单）。** README 给出内核菜单里的确切路径：

[platform/kv260/README.md:L124-L132](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L124-L132) —— `petalinux-config -c kernel` 打开内核 menuconfig，沿 `Device Drivers → Misc devices → <*> Xilinx Deep learning Processing Unit (DPU) Driver` 把 DPU 驱动设为内置（`<*>`，而非模块 `<M>`）。设为内置的好处是开机即用、无需 `modprobe`。

> 注意：脚本第 88 行同样调用了 `petalinux-config -c kernel`，但脚本是**非交互**运行的，其 DPU 驱动开关实际由第 52 行拷入的 `recipes-vai-kernel`（仓库外，待确认其确切内容）以配置片段形式提供。两条路径择一即可：手动构建按 README 走菜单，自动构建依赖 recipes 层。

**设备树与内核如何认识 DPU。** `get-hw-description` 把 `.xsa` 里 DPU 的 AXI 从机地址、中断号写进设备树（`system-user.dtsi` 一类），DPU 驱动按这些属性 `probe` 设备，暴露一个字符设备节点。后续 u5-l4 的 `xdputil query` 与 u7 的推理程序都经此节点访问 DPU。

#### 4.2.4 代码实践

**实践目标：** 厘清 `.xsa` → 设备树 → DPU 驱动 probe 这条信息流，确认 DPU 在内核侧「被发现、被驱动」的依据。

**操作步骤：**

1. 在 `helper_build_bsp.sh` 里找到导入 `.xsa` 的那一行（`--get-hw-description`），记下它指向哪个目录、对应 u5-l2 的哪个产物。
2. 对照 README 内核菜单路径，说明若不走脚本、纯手动构建，你需要在 menuconfig 里点亮哪一项。
3. （源码阅读型）说明：脚本第 52 行拷入的 `recipes-vai-kernel` 在本仓库中并不存在——指出它来自哪里、扮演什么角色（**待本地确认**：实际内容需到上游 `VAI-3.5-ZUP-DPU-TRD` 核对）。

**需要观察的现象 / 预期结果：**

- 你应当能复述：`.xsa` 提供「DPU 在哪」（地址/中断），内核菜单/recipes 提供「谁来驱动它」（DPU 驱动代码 + `CONFIG_*=y`）。两者缺一不可。
- 预期：成功构建并启动后，板载 `ls /dev/` 应出现 DPU 相关字符节点，`dmesg | grep -i dpu` 应看到驱动 probe 成功的日志（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1.** 为什么把 DPU 驱动设为 `<*>`（内置）而不是 `<M>`（模块）？

> **参考答案：** 内置意味着编进内核镜像 `image.ub`，开机即注册设备、无需额外加载。对嵌入式部署更省心——SD 卡上不必再带模块文件，启动流程更短、更不易出错。模块方式则需 rootfs 里部署 `.ko` 并在 init 脚本里 `modprobe`。

**练习 2.** 若忘了跑 `--get-hw-description`，DPU 还能被内核枚举到吗？

> **参考答案：** 不能（或地址错误）。设备树里 DPU 节点的 `reg`（AXI 地址）与 `interrupts` 来自 `.xsa`；不导入硬件描述，设备树就不知道 DPU 挂在哪个地址、哪根中断线，驱动 `probe` 时匹配不到设备，自然枚举失败。

---

### 4.3 镜像构建排错：`do_image_cpio` 与 SD 分区根文件系统

#### 4.3.1 概念说明

这是本讲最实用的一节。`PKG_OPTIONAL` 塞进 rootfs 的 Vitis AI 库、OpenCV、GStreamer、X11、on-target SDK 合计轻易超过 2GB。Yocto/PetaLinux 默认会为 INITRAMFS 用途生成多种 `cpio` 格式归档（`cpio`、`cpio.gz`、`cpio.gz.u-boot` 等）；当 rootfs >2GB 时，`do_image_cpio` 任务会失败，原因有二：cpio 归档无法塞进要链接进内核的 INITRAMFS（内存预算与格式限制），且超大 cpio 打包本身在工具链里不稳定。

修复策略有两条，且**互为表里**：

1. **删 cpio 产物**：`IMAGE_FSTYPES:remove = "cpio …"` 告诉 Yocto「别再生成任何 cpio 类型」，直接跳过 `do_image_cpio` 任务。
2. **改用 SD 卡 ext4 根文件系统**：既然不再用 INITRAMFS（不往内存塞 rootfs），把根设备指向 SD 卡第二分区 `/dev/mmcblk0p2`，rootfs 以 ext4 驻留磁盘。

脚本同时做了这两件事——这才是治本：大 rootfs 放磁盘、不再走 cpio。

#### 4.3.2 核心流程

可用一个不等式表达 INITRAMFS 路线的内存约束。KV260 的 DDR 容量有限（4 GB 量级），开机时 DDR 同时要容纳内核、initramfs 与 DPU 工作缓冲，可用余量约为

\[
\text{free} = \text{DDR} - (\text{kernel} + \text{initramfs} + \text{DPU buffers})
\]

当 \(\text{initramfs} > 2\,\text{GB}\) 时，\(\text{free}\) 所剩无几甚至为负，INITRAMFS 不可用——这正是 README 所述「`INITRAMFS cannot be used`」的物理含义。切到 SD 分区根文件系统后，\(\text{initramfs}\) 项归零（只保留一个极小的 initramfs 用于早期挂载），大 rootfs 落到磁盘，矛盾解除。

排错决策树：

```text
petalinux-build 报 do_image_cpio 失败？
├── 是 → 检查 rootfs 体积（多半 >2GB，因 PKG_OPTIONAL 一堆包）
│        ├── 治标：IMAGE_FSTYPES:remove = "cpio …"  （跳过 cpio 任务）
│        └── 治本：rootfs 由 INITRD 改 EXT4，根设备 = /dev/mmcblk0p2
├── 改完出现 do_populate_sysroot / lic_setscene 报错（lighttpd/mdadm/python3-* 等）？
│        └── README 经验：clean 后重跑多次 petalinux-build，最终能成（根因不明）
└── 否 → 继续打包 boot/wic
```

#### 4.3.3 源码精读

**README 记录的报错与手动修复。**

[platform/kv260/README.md:L140-L151](https://github.com/alan-turing-institute-sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L140-L151) —— 明确给出：`do_image_cpio` 失败的诱因是 rootfs >2GB 且 `INITRAMFS cannot be used`；修复方法是在 `project-spec/meta-user/conf/petalinuxbsp.conf` 追加两行 `IMAGE_FSTYPES:remove`（普通镜像 + debugfs 镜像各一行）。末尾还诚实记录了「删 cpio 后可能引发 `do_populate_sysroot`/`lic_setscene` 报错，反复 `petalinux-build` 最终能成，根因不明」——这是真实工程经验的痕迹，不是确定性规则。

**脚本里的「预防式」修复（治标）。**

[platform/kv260/sw/helper_build_bsp.sh:L60-L61](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L60-L61) —— 在 `customize_proj` 里**预先**把 README 的两行 `IMAGE_FSTYPES:remove` 追加进 `petalinuxbsp.conf`。也就是说：**只要用脚本构建，就天然避开了 `do_image_cpio`**；README 那段排错主要服务于「不走脚本、纯手动改工程」的情况。这是两份文档的分工。

**脚本里的「治本」修复：切到 EXT4 SD 根文件系统。**

[platform/kv260/sw/helper_build_bsp.sh:L69-L75](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L69-L75) —— 四条 `sed` 协同改写 `config`：关闭 `CONFIG_SUBSYSTEM_ROOTFS_INITRD`（不再用 INITRAMFS）、打开 `CONFIG_SUBSYSTEM_ROOTFS_EXT4`、删掉无用的 `INITRD_RAMDISK_LOADADDR`、把根文件系统格式收敛为 `ext4 tar.gz`，并把根设备显式钉死为 `/dev/mmcblk0p2`（SD 卡第二分区）。这组改动让超大 rootfs **驻留磁盘**而非内存，从根上解决了 cpio 不可用的问题。

**打包 boot 与 wic。**

[platform/kv260/sw/helper_build_bsp.sh:L90-L92](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/sw/helper_build_bsp.sh#L90-L92) —— 进入 `images/linux/` 后，`petalinux-package --boot` 把 FSBL（`zynqmp_fsbl.elf`）、U-Boot（`u-boot.elf`）、FPGA 比特流（`system.bit`）打包成 **`BOOT.BIN`**；`petalinux-package --wic` 生成可整体烧录的 **`petalinux-sdimage.wic`**。这步产生的 `BOOT.BIN` 与 `image.ub` 就是 SD 卡 boot 分区（FAT32）的成员；rootfs 则落到第二分区（ext4）。

**SD 卡分区与启动产物对照。**

[platform/kv260/README.md:L159-L167](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L159-L167) —— 两种烧录方式：(a) 用 Etcher/`dd` 直接烧 `petalinux-sdimage.wic`；(b) 手动分两区——分区 1（FAT32）放 `BOOT.BIN` + `image.ub` + `boot.scr`，分区 2（ext4）解压 `rootfs.tar.gz`。后者与脚本第 75 行钉死的 `/dev/mmcblk0p2` 完全吻合。

#### 4.3.4 代码实践

**实践目标：** 把「从 source settings.sh 到 petalinux-build」的完整命令序列写出来，并解释 `do_image_cpio` 报错的机理与 `IMAGE_FSTYPES:remove` 的缓解方式。

**操作步骤：**

1. 先读本节引用的 README 排错段落与脚本第 60-61、69-75、90-92 行。
2. 写出完整命令序列（见下方「示例」）。
3. 用一段话解释：为什么 rootfs >2GB 会触发 `do_image_cpio`，以及为什么删掉 cpio 类型 + 切 EXT4 分区能同时治标治本。

**完整命令序列（示例代码，非项目原脚本，按 README 与脚本逻辑整理）：**

```bash
# 0. 准备 PetaLinux 环境
source ~/petalinux/2023.2/settings.sh
echo $PETALINUX                      # 确认环境变量已设置

# 1. 进入 sw 目录，给脚本传板名（需先备齐 BSP 与 vai_petalinux_recipes/）
cd platform/kv260/sw
bash helper_build_bsp.sh kv260       # 自动 create_proj + customize_proj + build_proj
#   内部等价于：
#     petalinux-create -t project -s xilinx-kv260-v2023.2-10140544.bsp
#     sed … 改 rootfs_config / petalinuxbsp.conf / config  （含 IMAGE_FSTYPES:remove、EXT4 切换）
#     petalinux-config --get-hw-description=../hw/ --silentconfig
#     petalinux-config -c kernel          # 手动路径下在此菜单启 DPU 驱动
#     petalinux-build
#     petalinux-package --boot …
#     petalinux-package --wic

# 2. 若不走脚本、纯手动构建（README 路径）：
petalinux-config -c kernel           # Device Drivers → Misc devices → DPU Driver = <*>
# 手动把这两行加进 project-spec/meta-user/conf/petalinuxbsp.conf（脚本已自动加）：
#   IMAGE_FSTYPES:remove = "cpio cpio.gz cpio.bz2 cpio.xz cpio.lzma cpio.lz4 cpio.gz.u-boot"
#   IMAGE_FSTYPES_DEBUGFS:remove = "cpio cpio.gz cpio.bz2 cpio.xz cpio.lzma cpio.lz4 cpio.gz.u-boot"
petalinux-build

# 3. 产物在 images/linux/：BOOT.BIN、image.ub、boot.scr、rootfs.tar.gz、petalinux-sdimage.wic
```

**需要观察的现象 / 预期结果：**

- 构建日志末尾应出现 `Successfully copied ... rootfs.ext4` 之类的成功行，且 `images/linux/` 下能看到 `BOOT.BIN`、`image.ub`、`petalinux-sdimage.wic`。
- 若 `do_image_cpio` 报错，确认 `petalinuxbsp.conf` 是否真的少了那两行 `IMAGE_FSTYPES:remove`——脚本路径不会出现此错；手动路径漏加才会。
- 若删 cpio 后冒出 `do_populate_sysroot`/`lic_setscene` 报错，按 README 经验：`petalinux-build` 多重跑几次可解（**待本地验证**：根因 README 也未确定）。

> **为何 rootfs >2GB 触发 `do_image_cpio`、`IMAGE_FSTYPES:remove` 如何缓解：** `cpio` 归档本是 INITRAMFS 的载体，要被链接进 `image.ub` 并在开机时整块加载进 DDR。rootfs 超 2GB 后，一方面 cpio 工具打包超大归档不稳定，另一方面即便打成也无法作为 initramfs 塞进有限的 DDR（内核与 DPU 缓冲还要占内存）。`IMAGE_FSTYPES:remove = "cpio …"` 让 Yocto 跳过所有 cpio 派生类型的镜像任务，于是 `do_image_cpio` 根本不执行，报错消失；配合把根文件系统切到 SD 卡 ext4 分区（`/dev/mmcblk0p2`），大 rootfs 落磁盘、不再占内存，从而既绕开了 cpio、又保留了完整的 Vitis AI 运行环境。

#### 4.3.5 小练习与答案

**练习 1.** 既然脚本第 60-61 行已经删掉了 cpio 类型，为什么还要在第 71-75 行把 rootfs 从 INITRAMFS 改成 EXT4？只删 cpio 不够吗？

> **参考答案：** 只删 cpio 类型是「治标」——它让 `do_image_cpio` 任务不再执行，避免报错。但若仍保留 INITRAMFS 路线，系统会试图把 rootfs 加载进内存，>2GB 的 rootfs 在 4GB DDR 上不可行（内核与 DPU 缓冲无立足之地）。改成 EXT4 并指向 `/dev/mmcblk0p2` 是「治本」——rootfs 驻留 SD 卡分区，开机挂载即可，彻底解除内存约束。两步合用才完整。

**练习 2.** `BOOT.BIN` 里打包了哪三样东西？`image.ub` 又装了什么？

> **参考答案：** `BOOT.BIN` = FSBL（`zynqmp_fsbl.elf`）+ U-Boot（`u-boot.elf`）+ FPGA 比特流（`system.bit`），由 `petalinux-package --boot` 组装。`image.ub` 是 U-Boot 可识别的 FIT 镜像，装着 Linux 内核与（编译进内核的）设备树；在本项目的 EXT4 配置下，它不再携带大 rootfs（rootfs 在 SD 第二分区）。`boot.scr` 则告诉 U-Boot 如何加载 `image.ub`。

**练习 3.** README 说删 cpio 后可能冒出 `do_populate_sysroot` 报错、重跑几次能成且「根因不明」。作为读者，你会如何更系统地排查这类 Yocto 增量构建报错？

> **参考答案：** 建议的排查思路：用 `petalinux-build -c <出错的包>` 单包重编定位；用 `bitbake -c cleanall <包>` 清掉其 stamp 与缓存后重跑；检查 `tmp/work/` 下该包的 `log.do_populate_sysroot` 与 `temp/run.do_*` 脚本定位真正失败点；排查是否是「删 cpio 导致某包的某些输出文件消失、下游任务找不到」的连带反应。README 的「重跑即解」多半是 Yocto 增量缓存自愈，系统排查能避免反复碰运气。

## 5. 综合实践

把本讲三个模块串起来，完成一次「**给一份纯手动构建的 KV260 PetaLinux 工程补齐脚本自动化能力**」的源码阅读型任务。

**任务：** 假设你的同事完全照 README 手动步骤构建（source → petalinux-create → petalinux-config -c kernel 手点 DPU 驱动 → petalinux-build），结果在 `petalinux-build` 时撞上 `do_image_cpio` 报错。请你：

1. 定位 `helper_build_bsp.sh` 中**已预防**该报错的那两行（第 60-61 行），把它们的内容抄进手动工程的 `petalinuxbsp.conf`。
2. 进一步指出：光加这两行只是治标，你还要参照脚本第 71-75 行，在手动工程的 `config` 里完成「INITRD → EXT4、根设备 = `/dev/mmcblk0p2`」的等价改动，并解释为何必须连这一步一起做。
3. 解释为什么该同事的手动工程会报错、而用脚本构建不会——关键差异在于 `customize_proj` 在 `petalinux-build` **之前**就已经把修复写进了配置。
4. （进阶）列出该手动工程要真正跑通，除了本讲修复外，还缺哪两项**仓库外**的依赖（`xilinx-kv260-v2023.2-10140544.bsp` 与 `vai_petalinux_recipes/`），并说明各自来源。

**预期产出：** 一份「手动工程 → 可编译」的最小改动清单（2 行 `IMAGE_FSTYPES:remove` + 一组 `config` 的 EXT4 改动）+ 一段对「为何脚本不报错、手动会报错」的因果解释。

## 6. 本讲小结

- PetaLinux 把 u5-l2 导出的 `.xsa` 变成「跑得动 DPU 的嵌入式 Linux」；`helper_build_bsp.sh` 用 `create_proj`/`customize_proj`/`build_proj` 三段管道自动化了建工程、定制、编译打包全过程。
- 脚本禁用 `xrt`/`zocl` 以切到 **vivado flow**（DPU 走独立字符设备驱动，与 u5-l4 `is_vivado_flow:true` 自洽）；DPU 驱动的启用有「README 手动菜单」与「recipes-vai-kernel 自动片段」两条等价路径。
- rootfs 因塞入 Vitis AI/OpenCV/GStreamer/X11 等包而 **>2GB**，触发 `do_image_cpio` 报错（INITRAMFS 内存与格式双重不可用）。
- 修复一体两面：`IMAGE_FSTYPES:remove` 删 cpio 产物（治标、跳过失败任务）+ rootfs 由 INITRAMFS 切 EXT4、根设备钉死 `/dev/mmcblk0p2`（治本、rootfs 落磁盘）。
- 启动产物：`BOOT.BIN`（FSBL+U-Boot+比特流）、`image.ub`（内核+设备树）放 FAT32 第一分区；ext4 rootfs 放第二分区——与脚本 `CONFIG_SUBSYSTEM_SDROOT_DEV` 吻合。
- `.xsa`（u5-l2）→ `petalinux-config --get-hw-description`（本讲）是硬件到软件的唯一交接点；本讲产出的 SD 镜像 + DPU 驱动将被 u5-l4 的固件三件套加载与 `xdputil query` 验证所消费。

## 7. 下一步学习建议

- 继续本单元：进入 **u5-l4 固件制作与板载部署**，看本讲产出的 SD 镜像启动后，如何把加速器固件三件套（`project_1.bit.bin` + `kv260.dtbo` + `shell.json`）用 `xmutil loadapp` 加载、用 `xdputil query` 验证 DPU。
- 横向串联：结合 u5-l1 的 DPU IP 参数（`DPUCZDX8G_ISA1_B4096`、325 MHz、softmax 使能），你会看到它们在「TCL 配置 → 资源表 → 本讲内核驱动 → u5-l4 `xdputil query`」四处自洽闭环。
- 若要深入 Yocto：阅读 PetaLinux Tools Reference Guide（UG1144，README 已附链接），理解 `petalinuxbsp.conf`、`rootfs_config`、`config` 三个配置文件在 Yocto 层级中的位置，以及 `IMAGE_FSTYPES`、`IMAGE_INSTALL` 等 BitBake 变量的语义。
- 若对 vivado flow vs XRT flow 的差异感兴趣：对比 Xilinx 官方「DPU TRD（vivado flow，本讲）」与「Vitis 加速流（XRT/Zocl）」两套软件栈，理解为什么本项目选前者（更轻、更适合星载 <10W 部署）。
