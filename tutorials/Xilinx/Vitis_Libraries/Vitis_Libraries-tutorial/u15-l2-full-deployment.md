# 完整部署：hw 构建、SD 卡与已知问题

## 1. 本讲目标

本讲是「集成与部署」单元的第二讲，承接 [u13-l2 AIE 图主机控制与 SD 卡打包](./u13-l2-aie-host-packaging.md)（讲清了 `hw_emu` 下一张 SD 卡怎么打、怎么用 QEMU 跑）与 [u15-l1 依赖图与跨库组合](./u15-l1-cross-library-composition.md)（讲清了多库怎么拼进同一个工程）。本讲把视角推到**真正上板的 `hw` 构建**，以及围绕它的嵌入式平台依赖、SD 卡打包细节、平台重定向和已知问题。

学完后你应当能够：

- 说清 `hw_emu` 与 `hw` 两档构建的代价差异与各自产物；
- 解释嵌入式平台为什么必须额外准备 Common Image、sysroot、Image/rootfs 三件套；
- 看懂 `v++ --package` 在工程化 Makefile 里如何生成一张可启动的 SD 卡，以及 SD 卡上每个文件的作用；
- 用 `platform_map.json` 把逻辑平台名（如 `vck190`）重定向到具体 `.xpfm`，并知道仓库 README 里记录的已知问题与废弃库边界。

## 2. 前置知识

在进入本讲前，你需要先建立以下认知（若不熟悉请先回顾对应讲义）：

- **三档小写 target**：`sw_emu`（2025.1 起已移除）→ `hw_emu`（开发迭代默认，QEMU 跑整套系统）→ `hw`（真实上板交付），三段 `v++ -c/-l/--package` 流程的 `-t` 必须一致（[u5-l1](./u5-l1-vpp-l2-build.md)）。
- **PL+AIE 混合系统**：PL 的 `mm2s/s2mm` 搬数据、AIE 图做计算，二者在 `system.cfg` 里用 `sc=` 焊接（[u13-l1](./u13-l1-adf-graph-boundary.md)、[u13-l2](./u13-l2-aie-host-packaging.md)）。
- **xrt::graph 主机控制**：`reset()/run()/end()` 对应仿真侧的 `init()/run()/end()`，`--package.defer_aie_run` 把图启动权交给主机（[u13-l2](./u13-l2-aie-host-packaging.md)）。
- **跨库组合**：把多个库的 include 路径引入同一工程，依赖闭包由 `dependency.json` 决定（[u15-l1](./u15-l1-cross-library-composition.md)）。

本讲用到的两个关键术语：

- **sysroot**：交叉编译时，目标板（aarch64）的根文件系统镜像——里面是板子上 `/usr/include`、`/usr/lib` 的头文件与库（如 `libxrt_coreutil`、`libxilinxopencl`、`libadf_api_xrt`）。x86 主机编译器自带的是 x86 头文件，**无法**用来编译跑在板上的 `host.elf`，必须 `--sysroot` 指向 aarch64 sysroot。
- **Common Image**：AMD 为每类 Versal/嵌入式平台发布的「公共镜像包」，解压后同时提供 sysroot（交叉编译用）、`Image`（Linux 内核镜像）、`rootfs.ext4`（根文件系统）三样东西——它们正是 `--package` 打 SD 卡的输入。

## 3. 本讲源码地图

本讲以 dsp 库的 `vss_fft_ifft_1d`（PL+AIE 混合 FFT/IFFT 示例）为贯穿案例，配合顶层 README、vision README 与 `platform_map.json` 讲部署。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md) | 顶层 README，声明 2025.2 起废弃的 PL 库与不再支持的 Alveo 平台 |
| [platform_map.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json) | 4 个 Versal/AIE 逻辑平台名 → 真实 `.xpfm` 的映射表 |
| [dsp/L2/examples/vss_fft_ifft_1d/example.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk) | 简化版五目标 makefile：vss→xclbin→host→sd_card→run |
| [dsp/L2/examples/vss_fft_ifft_1d/Makefile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile) | 工程化版 makefile，含 DFX / AIE2PS / 通用嵌入式三条打包分支 |
| [dsp/L2/examples/vss_fft_ifft_1d/utils.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk) | 共享 makefile 片段：平台查找、sysroot/K_IMAGE/ROOTFS 派生、交叉编译器选择 |
| [dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/sdt_lopper.sh](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/sdt_lopper.sh) | 用 sdtgen+lopper+dtc 把 xsa 生成 `pl.dtbo` 设备树覆盖 |
| [dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/run_copy_wic.sh](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/run_copy_wic.sh) | 用 `wic cp` 把产物拷进 wic 磁盘镜像分区 |
| [dsp/L2/examples/vss_fft_ifft_1d/run_script.sh](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh) | 板上/QEMU 内执行 `host.elf` 并打印 `TEST PASSED/FAILED` 的脚本 |
| [dsp/L2/examples/vss_fft_ifft_1d/host.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp) | 原生 XRT 主机：`xrt::graph` 控制 + 与 ref_output 比对 |
| [vision/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md) | vision 库 README，含嵌入式公共镜像下载说明与 Known issues |

## 4. 核心概念与源码讲解

### 4.1 hw 构建代价与产出

#### 4.1.1 概念说明

Vitis 的三档小写 `target` 是一条「保真度与代价」逐级递增的阶梯：

- `hw_emu`（硬件仿真）：在主机上用 QEMU 跑起整套 Versal 系统，AIE 走周期近似模型、PL 走 RTL 仿真。**功能正确性可信，时序不可信**。开发迭代用它。
- `hw`（真实硬件）：跑完整的 Vivado 综合+实现+布局布线（place & route），产出真正能烧进 FPGA/AIE 的比特流。**时序、资源、频率都可信**。上板交付用它。

关键直觉是：`hw` 把 `hw_emu` 里「假装」跑的硬件真的造出来，所以代价从「分钟级～几小时」跳到「几小时～十几小时」。仓库的 CI 配置可以直接佐证这个量级。

#### 4.1.2 核心流程

`hw` 与 `hw_emu` 用的是**同一套三段流程**（`v++ -c` → `v++ -l` → `v++ --package`），区别只在 `-t` 参数从 `hw_emu` 改成 `hw`：

```
v++ -c  -t hw        # 编译每个内核 C++ -> XO（PL）或 libadf.a（AIE 图）
v++ -l  -t hw        # 按 system.cfg 链接成 .xsa（Versal）或 .xclbin（纯 PL）
v++ -p  -t hw        # 打包成 SD 卡（嵌入式）或 xclbin+PDI（PCIe）
```

在简化版 `example.mk` 里，`example_xclbin` 目标的 `-t` 写死为 `hw_emu`——这正是「从 hw_emu 切到 hw」要改的地方。

#### 4.1.3 源码精读

`example.mk` 里编译 PL 搬运内核的两条 `v++ -c` 都钉死 `-t hw_emu`：

[example.mk:31-34](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L31-L34) — 把 `mm2s`/`s2mm` 编译成 XO，`-t hw_emu` 决定保真度档位；切 `hw` 构建时这两行的 `-t hw_emu` 都要改成 `-t hw`，链接段 `v++ -l`（第 34 行）与打包段（第 42 行）也要同步改。

CI 代价可以从用例的元数据直接读到——`description.json` 的 `testinfo.jobs` 给 `vitis_hw_emu` 档预留了 `max_time_min: 470`（约 7.8 小时）和 `max_memory_MB: 40960`（40 GB）：

[description.json:32-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/description.json#L32-L37) — 这还只是 `hw_emu` 的预算；`hw` 因要做完整 Vivado 实现，时间通常更长（**待本地验证**：本仓库 CI 对该用例未声明 `hw` 档预算，故无法从源码给出确切数字）。

切到 `hw` 后，产出形态也变了——工程化 Makefile 在 `hw` + AIE2PS 平台分支里会**手动建一个 `sd_card/` 目录**，把 `dtbo`、`bif`、`pdi`、`xclbin`、`host.elf`、`run_script.sh` 拷进去：

[Makefile:209-222](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L209-L222) — `ifeq (${TARGET},hw)` 下 `cp -rf ...dtbo ...bif ...xclbin host.elf run_script.sh` 并 `mv ...pdi` 进 `sd_card/`，最后 `ln -sfn` 建一个 `package.hw` 软链。这就是「`hw` 产出 = 一整个可烧录目录」的来源。

而真正在板子上跑时，Makefile 不再像 `hw_emu` 那样自动启动，只打印一句提示让你手动拷卡：

[Makefile:272-279](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L272-L279) — `ifeq ($(TARGET),hw)` 且非 x86 时输出 `Please copy the content of sd_card folder and data to an SD Card and run on the board`。

#### 4.1.4 代码实践

**实践目标**：从源码读懂 `hw_emu` 的代价预算，并推断 `hw` 的额外代价。

**操作步骤**：

1. 打开 [description.json:32-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/description.json#L32-L37)，记下 `vitis_hw_emu` 的 `max_time_min` 与 `max_memory_MB`。
2. 在 `example.mk` 里数出 `v++` 被调用的总次数（第 32-34 行 + 第 42 行），理解为什么 `hw_emu` 都要 470 分钟。
3. 假想把所有 `-t hw_emu` 改成 `-t hw`，列出**新增**的重活（Vivado 综合、实现、布局布线、生成 PDI）。

**需要观察的现象**：`hw_emu` 已经需要约 7.8 小时；`hw` 在此基础上还要跑完整 place & route。

**预期结果**：`hw` 单次构建通常以「数小时」计，因此 CI 一般只对 `canary` 类用例跑 `hw`，日常开发停在 `hw_emu`。具体时长**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么本仓库的 `description.json` 只在 `testinfo.jobs` 里给 `vitis_hw_emu` 配了预算，没配 `hw`？

**参考答案**：该用例的 CI 定位是 `hw_emu` 回归（`targets: ["vitis_hw_emu"]`，见 [description.json:40-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/description.json#L40-L42)）。`hw` 构建代价过高，通常由专门的硬件流水线在变更合并后单独触发，不在每次提交的常规 CI 里。

**练习 2**：从 `example.mk` 的 `example_xclbin` 看，要把这个示例从 `hw_emu` 切到真实 `hw`，至少要改哪几行？

**参考答案**：第 32、33 行（两条 `v++ -c` 的 `-t hw_emu`）、第 34 行（`v++ -l` 的 `-t hw_emu`）、第 42 行（`v++ --package` 的 `-t hw_emu`）——三段的 `-t` 必须一致改为 `hw`。

---

### 4.2 嵌入式平台三件套：Common Image、sysroot、Image/rootfs

#### 4.2.1 概念说明

部署到 Versal/Zynq 这类嵌入式平台时，主机不再是 x86 PCIe 主机，而是板上的 ARM（aarch64）处理器。这带来两件 x86 流程没有的依赖：

1. **交叉编译主机程序**：`host.elf` 要跑在 aarch64 上，必须用 aarch64 交叉编译器，且需要目标板的头文件/库——即 sysroot。
2. **生成可启动的 SD 卡**：板子从 SD 卡启动，卡上必须有 Linux 内核（`Image`）、根文件系统（`rootfs.ext4`）、引导脚本，外加你的设计比特流与 `host.elf`。

这三样——sysroot、`Image`、`rootfs.ext4`——AMD 打包成 **Common Image** 一次性发布，需要在下载中心单独下载（详见 vision README）。

#### 4.2.2 核心流程

部署前的环境准备链：

```
下载 Common Image（按平台）
   ├── 解压 → 得到 sdk.sh / rootfs.ext4.gz / Image
   ├── ./sdk.sh -y -d ./ -p ...        # 安装 sysroot
   ├── gunzip rootfs.ext4.gz            # 解压根文件系统
   └── export SYSROOT=<安装后的 sysroot 路径>

Makefile 由 SYSROOT 自动派生：
   K_IMAGE = $(SYSROOT)/../../Image           # 内核镜像
   ROOTFS  = $(SYSROOT)/../../rootfs.ext4     # 根文件系统

交叉编译 host.elf：aarch64-linux-gnu-g++ --sysroot=$(SYSROOT) ...
打包 SD 卡：v++ -p --package.rootfs $(ROOTFS) --package.kernel_image $(K_IMAGE) --package.generate_sdcard
```

注意 `../../` 这个相对关系：sysroot 位于 Common Image 解压目录下的 `sdk/sysroots/.../`，往上两级正好回到 Common Image 根，那里放着 `Image` 和 `rootfs.ext4`。Makefile 正是靠这个固定布局，让你**只设一个 `SYSROOT`** 就够了。

#### 4.2.3 源码精读

`utils.mk` 里 `SYSROOT/K_IMAGE/ROOTFS` 的派生与校验：

[utils.mk:232-240](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L232-L240) — 非 x86 平台下，`K_IMAGE ?= $(SYSROOT)/../../Image`、`ROOTFS ?= $(SYSROOT)/../../rootfs.ext4`（`zc706` 老板用 `uImage`，其余用 `Image`）。`?=` 表示你可以覆盖，但默认就吃这个固定布局。

紧接着是「没设就报错退出」的护栏——`check_sysroot` 目标：

[utils.mk:242-247](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L242-L247) — `ifeq (,$(wildcard $(SYSROOT)))` 检查路径是否存在，不存在就 `$(error SYSROOT ENV variable is not set ...)`。`check_kimage`/`check_rootfs`（248-259 行）对 `K_IMAGE`/`ROOTFS` 做同样检查。这就是「忘了装 Common Image 就编译不过」的根因。

为什么必须交叉编译？看 `example.mk` 的 `example_host` 目标：

[example.mk:36-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L36-L37) — 用的是 `${XILINX_VITIS}/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-g++`（Vitis 自带的 aarch64 交叉 g++），带 `--sysroot=$(SYSROOT)`、`-I$(SYSROOT)/usr/include/xrt`（XRT 头件），并链 `-ladf_api_xrt`（AIE 图主机 API 库）。这些都是板上的库，只有 sysroot 里有。

`utils.mk` 还会根据 `HOST_ARCH` 自动选交叉编译器：

[utils.mk:283-287](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L283-L287) — `HOST_ARCH=aarch64` 时 `CXX := $(XILINX_VITIS)/gnu/aarch64/lin/aarch64-linux/bin/aarch64-linux-gnu-g++`；`aarch32` 选 `arm-linux-gnueabihf-g++`；`x86` 用本机 g++。`HOST_ARCH` 由 `platforminfo` 从 `.xpfm` 反查得到。

vision README 明确指出嵌入式平台需要单独下载 Common Image：

[vision/README.md:139](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L139) — `For embedded devices, platforms and common images have to be downloaded separately from the download center.`

#### 4.2.4 代码实践

**实践目标**：从源码理清「设一个 `SYSROOT`，自动得到三件套」的派生链。

**操作步骤**：

1. 读 [utils.mk:232-240](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L232-L240)，画出 `SYSROOT` → `K_IMAGE` / `ROOTFS` 的相对路径关系。
2. 读 [example.mk:36-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L36-L37)，找出三处用到 `$(SYSROOT)` 的编译/链接参数（`--sysroot`、`-I`、`-L`），解释去掉任何一个会发生什么。
3. 假设你的 Common Image 解压在 `/opt/common/`，写出正确的 `export SYSROOT=...`（提示：路径要落到 `sdk/sysroots/cortexa72-cortexa53-amd-linux` 这一级，参见 utils.mk 第 73 行的默认值风格）。

**需要观察的现象**：不设 `SYSROOT` 直接 `make` 会立刻在 `check_sysroot` 报错；设错路径（指到 Common Image 根而非 sysroot）则 `K_IMAGE`/`ROOTFS` 的 `../../` 拼接会落空。

**预期结果**：理解「`SYSROOT` 是交叉编译与打包的共同根，`Image`/`rootfs.ext4` 靠固定相对布局自动找到」。具体路径**待本地验证**（取决于你下载的 Common Image 版本与解压位置）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `K_IMAGE`/`ROOTFS` 用 `$(SYSROOT)/../../` 而不是各自独立的环境变量？

**参考答案**：因为 Common Image 的目录布局是固定的（sysroot 在 `sdk/sysroots/.../`，`Image`/`rootfs.ext4` 在解压根）。固定布局让用户只需设一个 `SYSROOT`，Makefile 用 `../../` 回到解压根就能定位另外两样，减少配置项。当然 `?=` 允许你显式覆盖。

**练习 2**：如果把 `example.mk:37` 的 `--sysroot=$(SYSROOT)` 去掉，编译还能过吗？为什么？

**参考答案**：大概率失败。`-I$(SYSROOT)/usr/include/xrt` 虽然能找到 XRT 头文件，但链接时需要的 aarch64 版 `libxrt_coreutil`、`libxilinxopencl`、`libadf_api_xrt`（`-L$(SYSROOT)/usr/lib`）若没有 `--sysroot`，交叉编译器会回退去系统默认搜索路径找 x86 版本，导致架构不匹配或找不到库。

---

### 4.3 SD 卡打包的工程化细节

#### 4.3.1 概念说明

u13-l2 已经用 `example.mk` 的简化版讲过 `--package` 怎么打 SD 卡。本节打开**工程化 Makefile**，看真实部署里 SD 卡到底装了什么、有几条不同的打包分支。

SD 卡上要同时容纳「能启动的 Linux 系统」+「你的硬件设计」+「你的应用程序」+「输入数据」，具体包括：

| 类别 | 文件 | 来源 |
| --- | --- | --- |
| 系统启动 | `Image`、`rootfs.ext4`、boot.scr 等 | `--package.rootfs` / `--package.kernel_image` 注入 |
| 硬件设计 | `*.xclbin` 或 `*.vss`、`*.pdi`（Versal 比特流）、`*.dtbo`（设备树覆盖） | `v++ -l`/`-p` 产出 |
| 应用 | `host.elf`（aarch64）、`run_script.sh`、`emconfig.json` | `--package.sd_file` 逐个加入 |
| 数据 | `input_front.txt`、`ref_output.txt` | `--package.sd_file` 或 `--package.sd_dir` |

#### 4.3.2 核心流程

`v++ --package`（别名 `-p`）的核心开关：

```
v++ -p -t <hw|hw_emu>
    --platform <xpfm>
    -o kernel.xclbin                           # 输出容器
    --package.out_dir package_hw_emu           # 输出目录
    --package.rootfs       $(ROOTFS)           # 注入根文件系统
    --package.kernel_image $(K_IMAGE)          # 注入 Linux 内核
    --package.generate_sdcard                  # 生成完整 SD 卡镜像
    --package.defer_aie_run                    # 把 AIE 图启动权交给主机
    --package.sd_file run_script.sh            # 逐个塞文件进 SD 卡
    --package.sd_file host.elf
    --package.sd_file data/input_front.txt
```

工程化 Makefile 按**平台类型**分三条打包分支：DFX 动态部分流、AIE2PS（Versal AI Edge / AIE-ML 类）流、通用嵌入式流。SD 卡的必要性本身由 `HOST_ARCH` 决定——aarch32/aarch64 才需要 SD 卡，x86 不需要。

#### 4.3.3 源码精读

先看简化版 `example.mk` 的 `example_sd_card`，它是最清晰的打包模板：

[example.mk:40-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L40-L42) — `emconfigutil` 先生成 `emconfig.json`（hw_emu 的设备仿真配置），然后 `v++ -p` 一次性把 xsa+libadf.a 打成 `kernel.xclbin` 并生成 SD 卡：`--package.rootfs`/`--package.kernel_image` 注入系统镜像，`--package.generate_sdcard` 生成完整卡，`--package.defer_aie_run` 交图启动权，一连串 `--package.sd_file` 把 `run_script.sh`、`host.elf`、`emconfig.json`、输入与参考输出都塞进去。

哪些架构需要 SD 卡，由 `utils.mk` 的 `SD_CARD_NEEDED` 逻辑决定：

[utils.mk:195-210](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L195-L210) — `HOST_ARCH` 为 `aarch32`/`aarch64` 时 `SD_CARD_NEEDED=on`，进而 `PACKAGE_NEEDED=on`；`pcie_versal`（Versal PCIe 卡）例外——它不需要 SD 卡，但仍需 `--package`。这就是「为什么 Alveo 卡不打包 SD 卡、Versal 板子才打包」的根因。

再看工程化 Makefile 的三条分支。**通用嵌入式流**（最常见）：

[Makefile:224](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L224) — `v++ -t $(TARGET) --platform $(XPLATFORM) -o ... -p ... --package.out_dir ... --package.rootfs $(ROOTFS) --package.generate_sdcard --package.kernel_image $(K_IMAGE) $(SD_FILES_WITH_PREFIX) $(SD_DIRS_WITH_PREFIX)`。和简化版几乎一致，只是参数化了。

**AIE2PS 流**（VEK280/VEK385 等 AIE-ML 平台）走完全不同的打包逻辑：

[Makefile:209-222](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L209-L222) — 用 `vpp -s -p -f`（`-s` 是该流的专用开关），**且 `hw` 下不靠 `--package.generate_sdcard`，而是手动 `mkdir sd_card`**，把 `*.dtbo`（设备树覆盖）、`*.bif`（启动镜像清单）、`xclbin`、`host.elf`、`run_script.sh` 拷进去，再把 `*.pdi`（Versal Programmable Device Image，即比特流）`mv` 进去。这告诉你：AIE-ML 类平台的 SD 卡结构和传统 Versal 不一样，`pdi`/`dtbo` 是它的特征文件。

`dtbo`（设备树 blob overlay）怎么来？由 `sdt_lopper.sh` 从链接产出的 `xsa` 生成：

[sdt_lopper.sh:53-59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/sdt_lopper.sh#L53-L59) — 三步：`sdtgen`（从 xsa 抽取 PL 信息生成设备树源）→ `lopper`（合并 board dtsi）→ `dtc -O dtb`（编译成 `pl.dtbo`）。这个 `pl.dtbo` 描述「PL 区有哪些 IP、地址映射」，Linux 启动时通过它识别你的加速器。

还有一种部署形态是把产物拷进现成的 wic 磁盘镜像分区（不用 `--package.generate_sdcard` 从零生成）：

[run_copy_wic.sh:31-40](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/run_copy_wic.sh#L31-L40) — 用 `wic cp --sector-size=4096` 把 xclbin、host.elf、dtbo、pdi、run_script.sh 逐一拷进 `${QEMU_COMBINED}/${WIC_PARTITION}`（wic 镜像的第 2 个分区）。这条路径用于「系统镜像固定、只换应用与设计」的场景。

SD 卡生成后怎么跑？`hw_emu` 下用 QEMU 启动整张卡，`run_script.sh` 在卡的 Linux 里执行 `host.elf`：

[run_script.sh:17-32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh#L17-L32) — 设 `LD_LIBRARY_PATH`、`XCL_EMULATION_MODE=hw_emu`、`XILINX_VITIS=/mnt`、`XILINX_XRT=/usr`，写 `platform_desc.txt` 到 `/etc/xocl.txt`，然后 `./host.elf`，按返回码打印 `TEST PASSED, RC=0` 或 `TEST FAILED`。真实 `hw` 下，这个脚本由你在板子的串口/SSH 里手动执行。

#### 4.3.4 代码实践

**实践目标**：把「SD 卡上每个文件」与「它在打包命令里的来源」一一对应。

**操作步骤**：

1. 打开 [example.mk:40-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L40-L42)，列出所有 `--package.sd_file` 与一个 `--package.sd_dir`，各写一句话说明它把什么放上了 SD 卡。
2. 对照 [Makefile:209-222](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L209-L222)，指出 AIE2PS 流里多出的两个文件 `*.pdi` 与 `*.dtbo` 各代表什么。
3. 读 [sdt_lopper.sh:53-59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/sdt_lopper.sh#L53-L59)，写出 `xsa` → `pl.dtbo` 的三步工具链。

**需要观察的现象**：`--package.sd_file` 是「逐文件上车」；`--package.generate_sdcard` 是「整卡从零生成」；AIE2PS 流不走后者而是手动 `cp`。

**预期结果**：理解 SD 卡 = 系统镜像 + 设计（xclbin/pdi/dtbo）+ 应用（host.elf/run_script.sh）+ 数据，且传统 Versal 与 AIE-ML 平台打包路径不同。

#### 4.3.5 小练习与答案

**练习 1**：`--package.defer_aie_run` 这个开关在部署里起什么作用？

**参考答案**：它让 AIE 图**不在 PDI 加载时自动启动**，而是把启动权交给主机程序的 `xrt::graph::run()`。这样主机可以先把输入数据备好、PL 搬运内核就位，再点火 AIE 图，与 [host.cpp:121-125](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L121-L125) 的 `my_graph.reset(); my_graph.run(NUM_ITER);` 对应。

**练习 2**：为什么 `run_copy_wic.sh` 里要 `unset LD_LIBRARY_PATH`？

**参考答案**：该脚本在主机上用 `wic cp` 操作磁盘镜像，但随后要 `source` Yocto SDK 的环境脚本（第 28 行）。主机里残留的 `LD_LIBRARY_PATH` 可能指向 x86 库，会污染交叉编译/工具链环境，故先清空再重新 source。

---

### 4.4 平台重定向与已知问题

#### 4.4.1 概念说明

部署时你面对两类「坑」：一是平台名与版本漂移——脚本里写 `vck190`，但真实 `.xpfm` 带版本号（如 `xilinx_vck190_base_202610_1`），不同 Vitis 版本号不同；二是仓库本身的已知问题与废弃边界——哪些库/平台已经不支持了。

仓库用两个机制应对：`platform_map.json` 把逻辑名映射到带版本的真实平台名，让脚本与版本解耦；README 集中记录已知问题与废弃清单。

#### 4.4.2 核心流程

平台重定向的寻址链（u2-l1 已讲三级兜底，这里聚焦映射表）：

```
你在脚本里写：PLATFORM=vck190          （逻辑名）
   ↓ platform_map.json 查表
真实平台名：xilinx_vck190_base_202610_1
   ↓ utils.mk 平台查找（PLATFORM_REPO_PATHS → Vitis 安装目录 → /opt/xilinx/platforms）
定位到 .xpfm 文件 → platforminfo 反查 part / HOST_ARCH / AIE_TYPE
```

已知问题与废弃边界的查阅路径：

```
顶层 README        → 2025.2 起废弃的 7 个 PL 库 + 不再支持的 Alveo 卡
vision/README.md   → vision 库的 Known issues（哪些用例 hw_emu 慢/失败）
各库 README        → 该库特有的限制
```

#### 4.4.3 源码精读

`platform_map.json` 全文只有 4 条映射，且**只覆盖 Versal/AIE 平台**（PL 的 Alveo 卡不在此列）：

[platform_map.json:1-6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6) — `vck190 → xilinx_vck190_base_202610_1`、`vck190_dfx → xilinx_vck190_base_dfx_202610_1`、`vek280 → xilinx_vek280_base_202610_1`、`vek385 → vek385_base`。注意 `_202610_1` 是版本号，换 Vitis 版本时这张表是首要更新点。

`utils.mk` 的平台查找是个多级 `wildcard` 兜底——优先吃 `PLATFORM_REPO_PATHS`，再退到 Vitis 安装目录的 `platforms/`、`base_platforms/`，最后退到 `/opt/xilinx/platforms/`，每级都先试精确名再试 awk 模式匹配：

[utils.mk:70-103](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L70-L103) — 第 70 行先看 `PLATFORM` 是不是 `.xpfm` 全路径（是就直接用）；否则按上面四级搜索。所以你写 `PLATFORM=vck190` 时，它靠 awk 模式匹配到 `xilinx_vck190_base_202610_1.xpfm`。

> ⚠️ 注意：`platform_map.json` 的映射是给**仓库脚本/CI**用的逻辑名规范，而 `utils.mk` 的查找是给**最终 makefile**用的；两者协同——脚本层用逻辑名，构建层靠模糊匹配落地到真实 `.xpfm`。

顶层 README 集中声明了 2025.2 的废弃边界。**PL 库废弃清单**（AIE 库不受影响）：

[README.md:22-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L22-L31) — `codec`、`data_analytics`、`data_compression`、`graph`、`hpc`、`quantitative_finance`、`sparse` 自 2025.2 起不再维护；老版本仍可从历史 release 取用；**AI Engine 库不受影响**。

**Alveo 平台废弃清单**：

[README.md:35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L35) — `U200`、`U250`、`U280` 不再支持，改用 `U50`、`U50LV`、`U55C` 实现 PL 设计（性能相近）。

各库 README 记录自己的已知问题。以 vision 为例：

[vision/README.md:203-209](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L203-L209) — 几个 AIE-ML 用例因输入大、`hw_emu` 很慢；`lkdensepyroptflow` 开 URAM 时序不过；`lkdensepyrof_uram`、`tonemapping`、`meanstddev-pipeline`、`hls2rgb aiesim`、`stereo-pipeline-URAM` 等 case 因已知工具问题在 `hw_emu` 失败，但其他 target 正常。这类信息直接决定你部署时该选哪个 target、该不该开 URAM。

#### 4.4.4 代码实践（本讲指定实践）

**实践目标**：依据 `platform_map.json` 把 `vck190` 映射到具体 `.xpfm`，并说明嵌入式 `hw` 构建为何需要额外准备 sysroot/Image/rootfs。

**操作步骤**：

1. 读 [platform_map.json:1-6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6)，写出 `vck190` 对应的真实平台名（`xilinx_vck190_base_202610_1`），并指出对应的 `.xpfm` 文件名是 `xilinx_vck190_base_202610_1.xpfm`。
2. 对照 [utils.mk:76-103](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L76-L103)，说明若你已 `export PLATFORM_REPO_PATHS=/opt/xilinx/platforms`，写 `PLATFORM=vck190` 会如何被 awk 模式匹配到该 `.xpfm`。
3. 回答关键问题：为什么嵌入式 `hw` 构建需要 sysroot/Image/rootfs，而 x86 PCIe Alveo 卡不需要？参考 [utils.mk:195-210](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L195-L210)（`SD_CARD_NEEDED`）、[utils.mk:232-240](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L232-L240)（sysroot 派生）与 [example.mk:36-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L36-L37)（交叉编译）。

**需要观察的现象**：`vck190` 这个短名在脚本里可用，但落地必须靠映射表 + 模糊匹配找到带版本号的 `.xpfm`。

**预期结果**：

- `vck190 → xilinx_vck190_base_202610_1.xpfm`。
- 嵌入式板卡的「主机」是板上的 aarch64 ARM，它从 SD 卡启动 Linux；因此必须 (a) 用 aarch64 交叉编译器 + sysroot 编译 `host.elf`，(b) 把 Linux 内核 `Image` 与根文件系统 `rootfs.ext4` 一起打进 SD 卡（`--package.generate_sdcard`）。x86 PCIe Alveo 卡的主机就是跑构建的那台 x86 机器，`host.elf` 用本机 g++ 编译、通过 XRT PCIe 驱动加载 xclbin，**既不需要交叉编译也不需要 SD 卡**，故不需要 sysroot/Image/rootfs。

#### 4.4.5 小练习与答案

**练习 1**：你在 2026.1 的 Vitis 上跑 `PLATFORM=vck190`，构建报「No platform matched pattern 'vck190'」。最该先检查哪两处？

**参考答案**：先查 [platform_map.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json) 里 `vck190` 对应的真实平台名是否在你已安装的平台里（版本号 `_202610_1` 是否匹配你的 Vitis 版本）；再确认 `PLATFORM_REPO_PATHS` 是否指向存放该 `.xpfm` 的目录（参见 utils.mk 第 76-83 行的搜索逻辑）。

**练习 2**：一个老项目用了 `sparse` 库，升级到 2025.2 后还能用吗？

**参考答案**：`sparse` 是 [README.md:22-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L22-L31) 列出的 7 个自 2025.2 起不再维护的 PL 库之一。2025.2 及以后的仓库里它不再维护，但可从历史 release 取用；若你的项目是 AIE 路线则不受此废弃影响。

---

## 5. 综合实践

**任务**：为一个「跑在 VCK190 上的 vss_fft_ifft_1d」拼出完整的部署清单，并预判会踩的坑。

请按顺序完成：

1. **平台重定向**：查 `platform_map.json`，写出 `vck190` → 真实 `.xpfm` 名；并说明 `utils.mk` 如何在 `PLATFORM_REPO_PATHS` 下用它做模糊匹配（参考 [utils.mk:76-103](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L76-L103)）。
2. **环境三件套**：写出 `export SYSROOT=...`，并推出 Makefile 会从它派生出 `K_IMAGE` 与 `ROOTFS` 的路径（参考 [utils.mk:232-240](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L232-L240)）；解释为什么缺了它 `check_sysroot` 会直接 `$(error)`（参考 [utils.mk:242-247](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L242-L247)）。
3. **构建序列**：列出从源码到上板的 make 目标顺序（参考 [example.mk:49](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L49) 的 `all` 依赖：`vss example_xclbin example_host example_sd_card example_run`），并指出切到真实 `hw` 时哪几处 `-t hw_emu` 要改成 `-t hw`。
4. **SD 卡内容**：逐项列出 `example_sd_card` 产出的 SD 卡上有哪几类文件、各自来源（参考 [example.mk:40-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L40-L42)）；并与 AIE2PS 流的 `pdi`/`dtbo` 手动拷贝对比（参考 [Makefile:209-222](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L209-L222)）。
5. **结果校验**：说明 `run_script.sh` 在板上执行 `host.elf` 后，靠什么字符串判定 PASS/FAIL（参考 [run_script.sh:26-30](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh#L26-L30)），以及 `host.cpp` 内部用 `ref_output.txt` + `level=256` 的非 bit 精确比对（参考 [host.cpp:226-252](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/host.cpp#L226-L252)）。
6. **规避坑**：依据 [README.md:35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L35) 说明若改用 Alveo U280 会怎样；依据 [vision/README.md:203-209](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L203-L209) 说明「hw_emu 失败但其他 target 正常」时该如何处置。

**预期产出**：一张覆盖「平台映射 → 三件套 → 构建序列 → SD 卡内容 → 校验 → 规避」的部署检查表，能作为你第一次上板 VCK190 的 runbook。具体每步耗时与是否一次通过**待本地验证**。

## 6. 本讲小结

- `hw` 与 `hw_emu` 用同一套 `v++ -c/-l/-p` 流程，区别只在 `-t`；`hw` 多跑完整 Vivado 实现，代价从「分钟级」跳到「数小时级」，CI 的 `hw_emu` 预算已达 470 分钟（[description.json:32-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/description.json#L32-L37)）。
- 嵌入式平台（aarch32/aarch64）必须额外准备 Common Image 三件套——sysroot、`Image`、`rootfs.ext4`；Makefile 靠固定相对布局让一个 `SYSROOT` 自动派生出 `K_IMAGE`/`ROOTFS`（[utils.mk:232-240](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L232-L240)），缺失则 `check_sysroot` 直接报错。
- `host.elf` 必须用 aarch64 交叉编译器配 `--sysroot` 编译，链 `-ladf_api_xrt`（[example.mk:36-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L36-L37)）。
- SD 卡 = 系统镜像 + 设计（xclbin/pdi/dtbo）+ 应用（host.elf/run_script.sh）+ 数据；`--package.generate_sdcard` 从零生成，AIE2PS 平台则手动拷 `pdi`/`dtbo`（[Makefile:209-222](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L209-L222)），`dtbo` 由 `sdt_lopper.sh` 从 xsa 生成（[sdt_lopper.sh:53-59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/scripts_mk/sdt_lopper.sh#L53-L59)）。
- `platform_map.json` 把 4 个 Versal/AIE 逻辑名映射到带版本号的真实 `.xpfm`（[platform_map.json:1-6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6)），`utils.mk` 用四级 wildcard + awk 模糊匹配落地（[utils.mk:70-103](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L70-L103)）。
- 2025.2 起 7 个 PL 库（codec/data_analytics/data_compression/graph/hpc/quantitative_finance/sparse）废弃、Alveo U200/U250/U280 不再支持（[README.md:22-35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/README.md#L22-L35)）；各库 Known issues（如 vision）直接决定选哪个 target、是否开 URAM（[vision/README.md:203-209](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L203-L209)）。

## 7. 下一步学习建议

本讲是「集成与部署」单元也是整本手册的收尾。建议你：

1. **真正上一次板**：挑一个 `category: canary` 的 AIE 用例（如 `vss_fft_ifft_1d`），在 VCK190/VEK280 上完整走一遍 `hw_emu → hw` 流程，亲手摸到 SD 卡、串口启动与 `TEST PASSED`，把本讲的「待本地验证」逐条替换成实测数字。
2. **横向对照 PCIe 流程**：找一个 Alveo U50 上的纯 PL 用例（如 vision L2），对比它**没有 SD 卡、没有 sysroot、host 用本机 g++ 编译**的简化部署，巩固「嵌入式 vs PCIe」的边界。
3. **回到依赖图做大型组合**：结合 [u15-l1](./u15-l1-cross-library-composition.md) 的跨库组合，尝试把 utils + data_mover + dsp 的闭包同时引入一个工程并部署，体会多库 include 路径在真实打包里的组织方式。
4. **关注版本漂移**：随着 Vitis 版本升级，定期核对 `platform_map.json` 的 `.xpfm` 版本号与 README 的废弃清单，这是长期维护加速库工程最易踩坑的两处。
