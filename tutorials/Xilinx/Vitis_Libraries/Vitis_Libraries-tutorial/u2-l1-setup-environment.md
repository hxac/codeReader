# 搭建 Vitis/XRT 开发环境

## 1. 本讲目标

学完本讲后，你应该能够：

- 在自己的 Linux shell 里正确 `source` Vitis 与 XRT 的环境脚本，并理解它们注入了哪些环境变量。
- 说清楚 `PLATFORM`、`PLATFORM_REPO_PATHS`、`XPART` 这三个变量的作用与区别，知道什么时候该用哪一个。
- 用 `platform_map.json` 把逻辑平台名（如 `vck190`）映射到具体的 `.xpfm` 文件。
- 通过 `v++ --version`、`vivado -version`、`vitis-run --help` 验证工具链就绪，并能从 L1 Makefile 的 `check_vivado`/`check_vpp` 目标理解工具链依赖。

本讲是「环境搭建」的第一篇，承接 [u1-l2 单仓库结构与跨库配置](u1-l2-monorepo-layout.md) 里讲过的 `platform_map.json`、`library.json` 等跨库配置，把视角从「仓库骨架」推进到「在自己机器上把工具链跑起来」。

## 2. 前置知识

在动手之前，先建立三个直觉。

**第一，Vitis 加速库不是「装好就能用」的纯软件库。** 它的内核最终要变成 FPGA 比特流（PL 路线）或 AI Engine 图（AIE 路线），所以你本地必须有一整套 AMD 工具链：Vitis（含 `v++`、`vitis-run`、`vivado`）、XRT（Xilinx Runtime，主机端运行时）。这些工具不会自动出现在 `PATH` 里，需要手动 `source` 它们的初始化脚本。

**第二，「目标硬件」有两种指定方式。** 一种是用「平台（platform）」，即一个 `.xpfm` 文件，描述了一块板子的完整硬件上下文（含 FPGA part、DDR/HBM、时钟等）；另一种是用裸的「FPGA part 名」（如 `xcu200-fsgd2104-2-e`），只指定芯片型号。库里的 Makefile 同时支持这两种。

**第三，三个大写 TARGET 与小写 target 是两套流程。** 这是 [u1-l3](u1-l3-l1l2l3-and-pl-aie.md) 已经强调过的点：L1 走 HLS 流程（`csim`/`csynth`/`cosim`/`vivado_syn`/`vivado_impl`），L2/L3 走 Vitis 流程（`sw_emu`/`hw_emu`/`hw`）。本讲搭建的环境对两套流程都通用。

> 名词速查：
> - **Vitis**：AMD 的高层加速开发套件，包含编译器 `v++`、HLS 运行器 `vitis-run`、综合器 `vivado` 等。
> - **XRT（Xilinx Runtime）**：主机端运行时库，主机程序通过它驱动加速卡上的内核。
> - **`.xpfm`**：平台描述文件，一块板子一个。
> - **`XPART`**：裸 FPGA 器件型号字符串。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `utils/README.md` | 给出 OS、C++14、Vitis/XRT 版本要求，以及 `settings64.sh` 与 `PLATFORM_REPO_PATHS` 的官方写法。 |
| `security/README.md` | 同时 `source` Vitis 与 XRT 两个脚本的范例。 |
| `blas/README.md` | 给出用 `.xpfm` 全路径作为 `PLATFORM` 的范例。 |
| `platform_map.json` | 把逻辑平台名映射到 `.xpfm` 文件名，目前覆盖 4 个 Versal/AIE 平台。 |
| `utils/L1/tests/stream_dup/Makefile` | L1 用例的真实 Makefile，包含 `check_vivado`/`check_vpp` 工具检查、平台搜索顺序、`v++`/`vitis-run` 调用方式。 |
| `dsp/L2/examples/vss_fft_ifft_1d/example.mk` | AIE/嵌入式路线的额外环境依赖（`SYSROOT`、aarch64 交叉编译器、`aietools`）。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：版本基线 → 两个环境脚本 → 三个目标变量 → 平台映射 → 工具链验证。

### 4.1 操作系统、C++14 与工具版本基线

#### 4.1.1 概念说明

在 `source` 任何脚本之前，先确认机器的「地基」是否达标：操作系统版本、C++ 标准、Vitis/XRT 版本。这三个条件是所有库共同的硬性要求，写在每个库 README 的 `Requirements` 段里，表述高度一致。

#### 4.1.2 核心流程

基线检查可以归纳为一句话：**较新的企业级 Linux 发行版 + C++14 + Vitis 2022.2 及以上 + 匹配版本的 XRT**。其中「Vitis 版本与 XRT 版本必须匹配」是最容易踩坑的点——XRT 是运行时，Vitis 是开发时，二者来自同一个安装包家族，错配会导致主机程序链接失败或上板后无法识别内核。

#### 4.1.3 源码精读

utils README 的 Software Platform / Development Tools 段落明确了操作系统清单、C++14 与 Vitis 版本：

[utils/README.md:11-20](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L11-L20) — 上面这段规定了支持的操作系统（RHEL8.10、RHEL9.2/9.3/9.4/9.5、Ubuntu 22.04.3/4/5 LTS），要求编译期开启 C++14，并要求 Vitis 2022.2 及以上、配对版本的 XRT。

security README 与 blas README 给出完全一致的要求，互相印证：

[security/README.md:132-137](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/README.md#L132-L137) — security 库声明同样的操作系统清单与 C++14 要求，并说明它「继承 Vitis 与 XRT 的系统要求」。

[blas/README.md:29-39](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L29-L39) — blas 库再次确认同样的软件平台与「Vitis 2022.2 及以上 + 匹配 XRT」。

#### 4.1.4 代码实践

1. **目标**：确认本机基线达标。
2. **步骤**：
   - 查系统版本：`cat /etc/os-release`。
   - 查 C++ 编译器是否支持 C++14：`g++ --version`（需 6.0 以上；实际编译由 Vitis 自带的交叉/原生工具链完成，这里只是先确认主机侧）。
3. **需要观察的现象**：`os-release` 显示 RHEL 8.10/9.x 或 Ubuntu 22.04.x。
4. **预期结果**：系统落在 README 列出的发行版清单内。若不在（如 Ubuntu 20.04 或 CentOS 7），属未受支持配置，可能仍能跑但无保证。
5. 若你的发行版不在清单内，记下版本号，后续遇到问题时优先怀疑环境兼容性（待本地验证具体行为）。

#### 4.1.5 小练习与答案

- **练习**：为什么说「Vitis 版本与 XRT 版本必须匹配」是硬要求，而不是建议？
- **参考答案**：XRT 提供主机端运行时（`libxrt_coreutil` 等）和设备驱动，Vitis 提供编译期工具与头文件；二者通过特定的 ABI/接口版本耦合。版本错配会出现「编译通过但上板找不到内核」或「链接符号缺失」这类难定位的问题，所以官方要求成对安装。
- **练习**：C++14 是「最低」还是「最高」要求？
- **参考答案**：最低要求。库里用到了 C++14 的特性（如 `std::make_unique`、泛型 lambda），所以编译期必须 `-std=c++14` 或更高；用 C++17 亦可，但 C++11 不行。

### 4.2 settings64.sh 与 setup.sh：两个环境脚本

#### 4.2.1 概念说明

Vitis 与 XRT 安装后并不会自动修改你的 `PATH`，而是各自附带一个 shell 初始化脚本。**`source` 这两个脚本，是让 `v++`、`vivado`、`vitis-run` 等命令在当前 shell 可见的唯一标准方式。** 它们的本质是一组 `export` 语句，向环境注入若干以 `XILINX_` 开头的变量。

两个脚本各司其职：

| 脚本 | 来源 | 主要注入的变量 | 让谁可见 |
|------|------|----------------|----------|
| `Vitis/settings64.sh` | Vitis 安装包 | `XILINX_VITIS`、`XILINX_VIVADO` 等 | `v++`、`vitis-run`、`vivado`、`platforminfo` |
| `xrt/setup.sh` | XRT 安装包 | `XILINX_XRT` 等 | XRT 主机库、`xbutil` 等运行时工具 |

#### 4.2.2 核心流程

标准启动顺序是：

```
1. source <Vitis 安装路径>/2025.2/Vitis/settings64.sh   # 先 Vitis
2. source /opt/xilinx/xrt/setup.sh                       # 再 XRT
3. （可选）export PLATFORM_REPO_PATHS=...                # 指向平台文件目录
```

`source`（也可写成 `.`）与直接执行脚本的区别：`source` 在**当前** shell 里执行那些 `export`，所以变量在 `source` 之后依然有效；直接执行则会在子 shell 里生效、退出后失效。这也是为什么 README 全部用 `source` 而不是 `./`。

#### 4.2.3 源码精读

utils README 的 Shell Environment 段给出了最权威的两行写法：

[utils/README.md:48-58](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L48-L58) — 上面这段先用 `source .../Vitis/settings64.sh` 初始化 Vitis，再用 `export PLATFORM_REPO_PATHS=...` 指定平台目录；并明确：只有设了 `PLATFORM_REPO_PATHS`，Makefile 才能把 `PLATFORM` 当作「名字模式」来用，否则必须给 `.xpfm` 的全路径。

security README 同时 `source` 了两个脚本，体现了「Vitis + XRT 缺一不可」：

[security/README.md:146-153](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/security/README.md#L146-L153) — 这段连续 `source` 了 `Vitis/settings64.sh` 与 `xrt/setup.sh` 两个脚本，是跨库通用的最简启动组合。

要理解这些脚本「注入了什么」，直接看 L1 Makefile 如何使用它们的结果。stream_dup 的 Makefile 一上来就用到了 `XILINX_VIVADO` 与 `XILINX_VITIS`：

[utils/L1/tests/stream_dup/Makefile:52-60](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L52-L60) — 这段把 `$(XILINX_VIVADO)/bin` 加进 `PATH`（让 `vivado` 可见），并在存在 `$(XILINX_VITIS)/bin/ldlibpath.sh` 时用它补 `LD_LIBRARY_PATH`（让运行期找到 Vitis 动态库）。若没 `source settings64.sh`，这两个变量为空，后续 `check_vivado`/`check_vpp` 就会直接报错。

#### 4.2.4 代码实践

1. **目标**：验证 `source` 之后环境变量确实注入。
2. **步骤**：
   ```bash
   echo "before: XILINX_VITIS=${XILINX_VITIS:-<empty>}"
   source /opt/xilinx/2025.2/Vitis/settings64.sh
   source /opt/xilinx/xrt/setup.sh
   echo "after:  XILINX_VITIS=${XILINX_VITIS}"
   echo "after:  XILINX_XRT=${XILINX_XRT}"
   ```
   （把路径换成你本机的实际安装路径）
3. **需要观察的现象**：`before` 行打印 `<empty>`，`after` 两行打印出非空的安装目录。
4. **预期结果**：`XILINX_VITIS`、`XILINX_XRT` 均指向真实安装目录（如 `/opt/xilinx/Vitis/2025.2`、`/opt/xilinx/xrt`）。
5. 若 `after` 仍为空，说明 `source` 的路径不对或脚本执行失败；脚本本身通常会在出错时打印提示，留意其 stderr 输出（待本地验证具体路径）。

#### 4.2.5 小练习与答案

- **练习**：为什么用 `source` 而不是 `bash setup.sh`？
- **参考答案**：`bash setup.sh` 会在子进程里执行 `export`，脚本一退出变量就消失；`source`（或 `.`）在当前 shell 执行，`export` 的变量保留到当前 shell 关闭，这样后续的 `make` 才能继承到 `XILINX_VITIS` 等变量。
- **练习**：如果每次开终端都要敲这两行，怎样减少重复？
- **参考答案**：把两行 `source` + `export PLATFORM_REPO_PATHS` 写进 `~/.bashrc`（或一个独立的 `env.sh` 需要时 `source`）。注意写进 `.bashrc` 会让每个 shell 都加载 Vitis 环境，若机器上还跑别的工作可能产生干扰，按需取舍。

### 4.3 PLATFORM / PLATFORM_REPO_PATHS / XPART：告诉工具目标在哪

#### 4.3.1 概念说明

工具链就位后，下一个问题是「编译出来的内核要面向哪块硬件」。库里用三个变量来回答，优先级和语义各不相同：

- **`PLATFORM`**：最常用。可以是一个平台名（如 `xilinx_vck190_base_202610_1`）、一个名字模式（如 `u200.*xdma`，支持 awk 正则）、或一个 `.xpfm` 文件的全路径。
- **`PLATFORM_REPO_PATHS`**：当 `PLATFORM` 用作「名字/模式」时，告诉工具去哪些目录里找对应的 `.xpfm`。多个目录用冒号 `:` 分隔（类似 `PATH`）。
- **`XPART`**：直接给裸 FPGA 器件型号（如 `xcu200-fsgd2104-2-e`）。**一旦设置 `XPART`，`PLATFORM` 会被忽略**——它绕过平台，只面向芯片本身。

#### 4.3.2 核心流程

L1 Makefile 把「目标解析」做成了一套搜索算法。当用户没有给 `XPART` 时，按下面的顺序把 `PLATFORM` 解析成一个 `.xpfm` 文件，再从 `.xpfm` 里反查出 FPGA part：

```
输入：PLATFORM（名字 / 模式 / 路径），XPART（可选）

if 设了 XPART:
    直接用 XPART，跳过 PLATFORM        # check_part 走「Using part」分支
else:
    if PLATFORM 是一个已存在的文件路径:
        直接当 .xpfm 用
    else:                              # 把 PLATFORM 当名字/模式去搜
        1. 在 PLATFORM_REPO_PATHS 里搜（先精确名，后模式）
        2. 在 Vitis 安装目录搜（platforms/ 、base_platforms/）
        3. 在 /opt/xilinx/platforms/ 默认位置搜
        取第一个命中
    用 platforminfo 从 .xpfm 反查出 XPART
```

这个顺序解释了一个常见困惑：**即使不设 `PLATFORM_REPO_PATHS`，有时也能找到平台**——因为第 2、3 步会兜底去 Vitis 安装目录和 `/opt/xilinx/platforms` 找。但官方推荐显式设置 `PLATFORM_REPO_PATHS`，让结果可复现。

#### 4.3.3 源码精读

Makefile 的 `check_part` 目标把上面这套搜索算法落到了代码里。先看整体分支入口：

[utils/L1/tests/stream_dup/Makefile:87-106](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L87-L106) — 这段先判断「是否设了 `XPART`」；若没设，再判断 `PLATFORM` 是不是已存在的文件路径（是就直接用），否则把 `PLATFORM` 转小写后当作 `DEVICE_L`，进入「按名字/模式搜索」分支，第一步就是去 `PLATFORM_REPO_PATHS` 里找。

第 2、3 步兜底搜索与 `platforminfo` 反查 part：

[utils/L1/tests/stream_dup/Makefile:107-145](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L107-L145) — 这段先去 Vitis 安装目录（`platforms/`、`base_platforms/`，分别对应 vitis < 2022.2 与 ≥ 2022.2 的布局）和 `/opt/xilinx/platforms/` 兜底搜索；找到唯一的 `.xpfm` 后，调用 `$(XILINX_VITIS)/bin/platforminfo --json="hardwarePlatform.devices[0].fpgaPart"` 从平台里反查出 FPGA part 名，赋给 `XPART`。

`PLATFORM` 默认值本身也透露了仓库当前的取向——默认是一个 Versal/AIE 平台：

[utils/L1/tests/stream_dup/Makefile:50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L50) — `PLATFORM ?= xilinx_vck190_base_202610_1`，即不显式传 `PLATFORM` 时默认面向 VCK190（Versal）。

Makefile 的 `help` 段还明确说明了 `XPART` 与 `PLATFORM` 的优先级：

[utils/L1/tests/stream_dup/Makefile:35-39](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L35-L39) — 上面这段写明：`PLATFORM` 大小写不敏感、支持 awk 正则（如 `u200.*xdma`），也可直接给 `.xpfm` 全路径；而用 `XPART` 指定裸 part 时，`PLATFORM` 会被忽略。

而 blas README 给出了「直接给 `.xpfm` 全路径」这种最保险的用法：

[blas/README.md:82-85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L82-L85) — 这里 `PLATFORM=/path/to/xilinx_u250_gen3x16_xdma_3_1_202020_1.xpfm`，直接把绝对路径交给 `PLATFORM`，跳过任何搜索。

#### 4.3.4 代码实践

1. **目标**：体会三种指定目标的方式。
2. **步骤**（在仓库根目录 `source` 好环境后）：
   ```bash
   # 方式 A：名字模式 + REPO_PATHS（推荐）
   export PLATFORM_REPO_PATHS=/opt/xilinx/platforms
   make -C utils/L1/tests/stream_dup help          # 看 help 里对 PLATFORM 的说明

   # 方式 B：全路径
   ls /opt/xilinx/platforms/*/*.xpfm 2>/dev/null    # 列出本机已有的 .xpfm

   # 方式 C：裸 part
   make -C utils/L1/tests/stream_dup help           # help 里 XPART 的示例 xcu200-fsgd2104-2-e
   ```
3. **需要观察的现象**：方式 B 能列出本机已安装的平台文件；`make help` 文本与上面引用的源码一致。
4. **预期结果**：本机至少能找到一个 `.xpfm`；若一个都没有，说明只装了工具链没装平台包，需要额外下载平台。
5. 具体能列出哪些 `.xpfm` 取决于本机安装情况（待本地验证）。

#### 4.3.5 小练习与答案

- **练习**：同时设了 `PLATFORM=u200_xdma_201830_1` 和 `XPART=xcu200-fsgd2104-2-e`，最终面向哪个？
- **参考答案**：面向 `XPART` 指定的 `xcu200-fsgd2104-2-e`。Makefile 里 `XPART` 分支优先，`PLATFORM` 被忽略。
- **练习**：不设 `PLATFORM_REPO_PATHS`，`PLATFORM` 还可能被解析成功吗？
- **参考答案**：可能。Makefile 会兜底去 Vitis 安装目录（`platforms/`、`base_platforms/`）和 `/opt/xilinx/platforms/` 搜索。但显式设 `PLATFORM_REPO_PATHS` 能保证可复现，不依赖这些默认位置是否装有目标平台。

### 4.4 平台选择与 platform_map.json

#### 4.4.1 概念说明

`PLATFORM` 的值是一串平台文件名（如 `xilinx_vck190_base_202610_1`），又长又带版本号，不同 Vitis 版本下名字还会变。为了让示例脚本与平台版本解耦，仓库顶层放了一个 `platform_map.json`：它把一个**稳定的逻辑名**（如 `vck190`）映射到**当前版本下真实的 `.xpfm` 文件名**。脚本和文档用逻辑名，需要时再查表换成真实名。

需要特别注意：这个文件目前**只覆盖 Versal/AIE 平台**（PL 的 Alveo 卡不在此列），这呼应了 [u1-l1](u1-l1-project-overview.md) 讲过的「AIE 路线跑在 Versal 上」。

#### 4.4.2 核心流程

使用流程是：

```
逻辑名（脚本/文档里出现）  --查 platform_map.json-->  真实 .xpfm 名  --交给--> PLATFORM 变量
```

例如脚本里看到 `vck190`，查表得 `xilinx_vck190_base_202610_1`，再配合 `PLATFORM_REPO_PATHS` 让 Makefile 找到对应的 `.xpfm`。

#### 4.4.3 源码精读

整个文件只有 4 条映射，非常简短：

[platform_map.json:1-6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6) — 这 4 行把 `vck190`、`vck190_dfx`、`vek280`、`vek385` 四个逻辑名分别映射到 `xilinx_vck190_base_202610_1`、`xilinx_vck190_base_dfx_202610_1`、`xilinx_vek280_base_202610_1`、`vek385_base`。注意文件名里的 `202610` 是平台版本号，升级 Vitis 时这个映射会随之更新，而脚本里写的逻辑名不变。

把这 4 个名字与上一节 Makefile 的默认 `PLATFORM` 对照：`xilinx_vck190_base_202610_1` 正是 `platform_map.json` 里 `vck190` 的映射值，二者一致，说明 Makefile 默认值就是按这张表选的 Versal 平台。

#### 4.4.4 代码实践

1. **目标**：会用 `platform_map.json` 把逻辑名换成真实平台名。
2. **步骤**：
   ```bash
   # 在仓库根目录
   cat platform_map.json
   # 用 jq 或 grep 取出 vek280 的映射
   grep vek280 platform_map.json
   ```
3. **需要观察的现象**：`vek280` 对应 `xilinx_vek280_base_202610_1`。
4. **预期结果**：得到 4 条映射；若本机已装 VEK280 平台包，可接着 `export PLATFORM=xilinx_vek280_base_202610_1` 并 `export PLATFORM_REPO_PATHS=/opt/xilinx/platforms` 让 Makefile 解析到它。
5. 若本机未装对应平台包，`PLATFORM_REPO_PATHS` 指向的目录里不会有该 `.xpfm`，Makefile 会报 `XPART is not set and cannot be inferred`（待本地验证是否已装）。

#### 4.4.5 小练习与答案

- **练习**：为什么 Alveo U50 这类 PL 卡不在 `platform_map.json` 里？
- **参考答案**：该文件当前只收录 Versal/AIE 平台（用于 AIE/ADF 图路线）。PL 的 Alveo 卡直接用自己的 `.xpfm` 文件名（如 `xilinx_u50_gen3x16_xdma_201920_3`）传给 `PLATFORM` 即可，无需经过这张表。
- **练习**：升级 Vitus 后，`vck190` 对应的真实名变了，脚本要改吗？
- **参考答案**：不用。脚本只写逻辑名 `vck190`，查的是 `platform_map.json`；仓库升级时会同步更新这张表的真实名，对脚本透明。这也是这张表存在的核心价值。

### 4.5 验证 v++ / vivado / vitis-run 工具链

#### 4.5.1 概念说明

环境脚本 `source` 完、平台也准备好之后，最后一步是**主动验证三个关键可执行文件确实可用**：

- **`v++`**：Vitis 编译器，把内核源码编译成 XO、链接成 xsa、打包成 xclbin；在 L1 HLS 流程里还负责 HLS 编译（`--mode hls`）。
- **`vivado`**：底层综合/实现工具，`vivado_syn`/`vivado_impl` 这两个 TARGET 直接调用它。
- **`vitis-run`**：HLS 流程的运行器，负责跑 `csim`/`cosim` 等阶段（同样是 `--mode hls`）。

库的 Makefile 本身就有专门的检查目标来确认前两者存在，所以即使你忘了手动验证，`make` 也会替你检查——但提前手动验证能更快定位「是环境没配好，还是别的问题」。

#### 4.5.2 核心流程

```
v++ --version        # 打印 Vitis 版本，证明 v++ 在 PATH 且能执行
vivado -version      # 打印 Vivado 版本
vitis-run --help     # 打印帮助，证明 vitis-run 可用
（进阶）which v++ vivado vitis-run   # 确认它们来自 XILINX_VITIS/bin
```

#### 4.5.3 源码精读

stream_dup 的 Makefile 用 `check_vivado` 与 `check_vpp` 两个目标做存在性检查，是最权威的「工具该在哪」的定义：

[utils/L1/tests/stream_dup/Makefile:75-85](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L75-L85) — 上面这段用 `wildcard` 检查 `$(XILINX_VIVADO)/bin/vivado` 与 `$(XILINX_VITIS)/bin/v++` 是否存在；不存在就打印「Please set XILINX_VIVADO / XILINX_VITIS variable」并失败。可见这两个变量就是工具的根目录。

`v++` 与 `vitis-run` 真正被调用的地方在 `all`/`run` 目标里：

[utils/L1/tests/stream_dup/Makefile:179-187](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile#L179-L187) — 这段先用 `v++ -c --mode hls --config $(CONFIG_FILE) --work_dir $(WORK_DIR) --part $(XPART)` 做 HLS 编译（仅当 `TARGET` 不是 `csim` 时），再用 `vitis-run --mode hls --config $(CONFIG_FILE) --$(TARGET_REL) --work_dir $(WORK_DIR) --part $(XPART)` 跑具体阶段（仅当 `TARGET` 不是 `csynth` 时）。注意二者都带 `--mode hls`，这正是 L1 流程与 L2 流程（不带 `--mode hls`）的关键区别。

> 旁注：对 AIE/嵌入式路线（如 dsp 的 `vss_fft_ifft_1d`），工具链还要再扩两项——aarch64 交叉编译器与 `SYSROOT`（含 `Image`/`rootfs` 的 Common Image），因为主机程序要交叉编译到 ARM 上跑。下面这段就是证据：

[dsp/L2/examples/vss_fft_ifft_1d/example.mk:36-45](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L36-L45) — 上面这段用 `$(XILINX_VITIS)/gnu/aarch64/.../aarch64-linux-gnu-g++` 交叉编译主机程序，并带上 `--sysroot=$(SYSROOT)`、`-I ${XILINX_VITIS}/aietools/include`、`-ladf_api_xrt` 等 AIE 专属依赖；`example_sd_card` 目标还会用 `--package.rootfs`、`--package.kernel_image` 打包 SD 卡，并用 `launch_hw_emu.sh` 启动硬件仿真。本讲只要知道「AIE 路线比纯 PL 多这几样」即可，细节留到 [u13](u13-l2-aie-host-packaging.md) 讲。

#### 4.5.4 代码实践

1. **目标**：确认三个工具都可用并记录版本。
2. **步骤**（`source` 好两个脚本之后）：
   ```bash
   v++ --version
   vivado -version
   vitis-run --help | head -n 20
   which v++ vivado vitis-run
   ```
3. **需要观察的现象**：前三条各打印出版本号或帮助文本；`which` 显示三者都位于 `${XILINX_VITIS}/bin`（`vivado` 在 `${XILINX_VIVADO}/bin`）下。
4. **预期结果**：`v++ --version` 打印类似 `Vitis v++ Compiler v2025.2` 的字样；`vitis-run --help` 列出含 `--mode hls` 的选项；`which` 的输出目录与你 `source` 后的 `XILINX_VITIS` 一致。
5. 若某条命令 `command not found`，回到 4.2 检查 `source` 是否成功；若 `which` 指向非 Xilinx 目录，说明 `PATH` 被其他工具污染，需调整（待本地验证具体版本号）。

#### 4.5.5 小练习与答案

- **练习**：Makefile 里有 `check_vivado` 和 `check_vpp`，却没有 `check_vitis_run`，这是疏漏吗？
- **参考答案**：不是。`csim`（最常见的快速验证 TARGET）只走 `vitis-run` 那条路径，且 `vitis-run` 与 `v++` 来自同一个 Vitis 安装；`check_vpp` 已经确认了 `$(XILINX_VITIS)/bin` 可达，基本等价于确认 `vitis-run` 也在。不过手动 `vitis-run --help` 仍是好习惯。
- **练习**：为什么 `v++` 和 `vitis-run` 都要带 `--mode hls`？
- **参考答案**：`v++` 是个多模式工具（既能做 HLS，也能做内核编译/链接/打包）。`--mode hls` 显式告诉它「这次走 HLS 子流程」，对应 L1 的内核原语综合；L2 流程里 `v++ -c`/`v++ -l` 不带 `--mode hls`，走的是 Vitis 内核编译流程。

## 5. 综合实践

把本讲四个环节串起来，做一次「从零到就绪」的环境自检，并顺带读懂 Makefile 的目标解析逻辑。

**任务**：写一个 `env_check.sh` 脚本（这是本讲义编写的**示例脚本**，不是仓库已有文件），依次完成：

1. `source` Vitis 与 XRT 两个脚本（路径按你本机替换）。
2. `export PLATFORM_REPO_PATHS=/opt/xilinx/platforms`。
3. 打印 `XILINX_VITIS`、`XILINX_VIVADO`、`XILINX_XRT` 三个变量，确认非空。
4. 运行 `v++ --version`、`vivado -version`、`vitis-run --help | head`，确认三条都有输出。
5. 用 `grep` 在 `platform_map.json` 里查出 `vek280` 对应的真实平台名，并打印一行结论。

参考写法（示例脚本）：

```bash
#!/usr/bin/env bash
set -e
# 1. 初始化工具链（路径请按本机实际修改）
source /opt/xilinx/2025.2/Vitis/settings64.sh
source /opt/xilinx/xrt/setup.sh
# 2. 平台搜索根
export PLATFORM_REPO_PATHS=/opt/xilinx/platforms
# 3. 自检变量
echo "XILINX_VITIS  = ${XILINX_VITIS:?empty}"
echo "XILINX_VIVADO = ${XILINX_VIVADO:?empty}"
echo "XILINX_XRT    = ${XILINX_XRT:?empty}"
# 4. 自检工具
v++ --version
vivado -version
vitis-run --help | head -n 5
# 5. 查平台映射
echo "vek280 -> $(grep '\"vek280\"' "$(dirname "$0")/platform_map.json")"
```

**做完后再做一道源码阅读题**：打开 [utils/L1/tests/stream_dup/Makefile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile)，回答：

- `check_vivado` 与 `check_vpp` 分别检查哪个目录下的哪个可执行文件？
- 当用户既没设 `PLATFORM` 也没设 `XPART` 时，会用什么默认 `PLATFORM`？（提示：第 50 行）
- 平台搜索的三级兜底顺序是什么？（提示：第 98–135 行）

**预期结果**：脚本顺利跑完，结尾打印出类似 `vek280 -> "vek280": "xilinx_vek280_base_202610_1",` 的一行；三道阅读题的答案分别对应 `XILINX_VIVADO/bin/vivado` 与 `XILINX_VITIS/bin/v++`、默认 `xilinx_vck190_base_202610_1`、以及「`PLATFORM_REPO_PATHS` → Vitis 安装目录 → `/opt/xilinx/platforms`」。脚本中的安装路径与版本号取决于本机（待本地验证）。

## 6. 本讲小结

- 所有库共享同一套基线：受支持的 RHEL/Ubuntu 版本、C++14、Vitis 2022.2 及以上 + 匹配版本的 XRT。
- 让工具可见的唯一标准方式是 `source` 两个脚本：`Vitis/settings64.sh`（注入 `XILINX_VITIS`/`XILINX_VIVADO`）与 `xrt/setup.sh`（注入 `XILINX_XRT`）。必须用 `source` 而非直接执行。
- 指定目标硬件有三个变量：`PLATFORM`（名字/模式/路径）、`PLATFORM_REPO_PATHS`（搜索根）、`XPART`（裸 part，优先级最高，设了会忽略 `PLATFORM`）。
- L1 Makefile 把目标解析做成三级兜底搜索（`PLATFORM_REPO_PATHS` → Vitis 安装目录 → `/opt/xilinx/platforms`），并用 `platforminfo` 从 `.xpfm` 反查 part。
- `platform_map.json` 把 Versal/AIE 逻辑名（`vck190`/`vek280` 等）映射到带版本号的真实 `.xpfm` 名，让脚本与平台版本解耦。
- 工具链验证就是确认 `v++`、`vivado`、`vitis-run` 三者可用，它们都来自 `${XILINX_VITIS}/bin`（`vivado` 在 `${XILINX_VIVADO}/bin`）；Makefile 的 `check_vivado`/`check_vpp` 已内置前两者的存在性检查。

## 7. 下一步学习建议

环境就绪后，下一讲 [u2-l2 运行第一个 HLS L1 用例：stream_dup](u2-l2-first-hls-case.md) 会带你真正跑通一个 L1 用例（`make run TARGET=csim`），把本讲配好的环境用到实处。

若想先从源码层面巩固「工具链如何被调用」，可以直接精读：
- [utils/L1/tests/stream_dup/Makefile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/tests/stream_dup/Makefile)（`check_*` 与 `v++`/`vitis-run` 调用）。
- [dsp/L2/examples/vss_fft_ifft_1d/example.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk)（AIE 路线多出的交叉编译与 SD 卡打包）。
