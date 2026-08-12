# v++ L2 构建流程：XO→xsa→xclbin

## 1. 本讲目标

本讲解决一个核心问题：**L2 层的一个内核（或一组内核）是如何从 C++ 源码变成可以加载到加速卡上运行的 `xclbin` 的？**

读完本讲，你应该能够：

- 说清 `v++ -c`（编译）、`v++ -l`（链接）、`v++ --package`（打包）三段流程各自的输入、输出和职责。
- 区分三种产物 `XO`、`xsa`、`xclbin` 之间谁包含谁的关系。
- 理解 `system.cfg` 在链接阶段如何描述「内核实例、存储端口、流连接」。
- 区分 `sw_emu`/`hw_emu`/`hw` 三种 target 的保真度与代价，并知道当前工具版本里 `sw_emu` 的真实状态。
- 对照 `example.mk` 手写从 XO 到上板 xclbin 的三条 `v++` 命令。

本讲承接 u2-l3（L1 的 HLS TARGET 流程）与 u4-l2（主机端 XRT 控制链）。L1 用大写 TARGET（`csim`/`csynth`/...）验证单个 HLS 函数；L2 用小写 target（`hw_emu`/`hw`）把多个内核组装成可上板的系统。两套流程的边界在这里再次出现，请时刻区分。

---

## 2. 前置知识

### 2.1 从「一个函数」到「一个系统」

在 u3 单元里，我们关注的是**一个 HLS 内核函数**：给它 `hls::stream`，它做计算，再吐 `hls::stream`。那是 L1 的世界。

但加速卡上真正跑起来的，从来不是孤零零的一个函数，而是一个**系统**：

- 几个 PL 内核（如搬数据的 `mm2s`/`s2mm`）；
- 可能还有一个 AIE 图（在 Versal 的 AI Engine 阵列上做计算）；
- 它们之间用 AXI Stream 连起来；
- 主机程序通过 XRT 加载一个 `xclbin` 文件来驱动整个系统。

L2 的任务，就是把「一堆源码」组装成「一个可加载的系统镜像」。这个组装过程，就是本讲的主角——`v++` 工具链的三段式流程。

### 2.2 三个关键名词

| 名词 | 全称 / 含义 | 直觉比喻 |
|------|------------|---------|
| **XO** | Xilinx Object，单个内核编译后的封装 | 一个 `.o` 目标文件 |
| **xsa** | Xilinx Support Archive，链接后的硬件容器 | 把多个 `.o` 链接成的「半成品硬件」 |
| **xclbin** | Xilinx Cloud Binary，最终可加载的加速二进制 | 最终的「可执行程序」 |

### 2.3 `v++` 是什么

`v++`（也叫 Vitis 编译器，vitis makefile-generator 里把它记作变量 `VPP`）是 L2/L3 的统一入口命令。它能做三件事，分别对应三个子命令：

- `v++ -c`：**编译**（compile）一个内核源码为 XO；
- `v++ -l`：**链接**（link）多个 XO（加上 AIE 容器）为 xsa；
- `v++ --package`（简写 `-p`）：**打包**（package）xsa 为最终的 xclbin，甚至整张 SD 卡。

记忆口诀：**编译 `-c`、链接 `-l`、打包 `-p`**——三个字母对应 `compile`、`link`、`package`。

> 注意：L1 流程里也出现 `v++ -c`（u2-l3 讲过，`v++ -c --mode hls` 跑 csynth）。本讲的 `v++ -c` 是 **L2 的内核编译**（不带 `--mode hls`，默认就是 `--mode xbb`/内核模式）。两者是同一个工具的不同模式，输入都是 C++，但产物和后续流程不同。

---

## 3. 本讲源码地图

本讲以 `dsp` 库的 **`vss_fft_ifft_1d`** 示例为贯穿案例。这是一个 PL+AIE 混合系统（PL 搬数据 + AIE 做 FFT/IFFT），它的构建文件集合最能体现「三段流程」的全貌。

| 文件 | 作用 |
|------|------|
| [dsp/L2/examples/vss_fft_ifft_1d/example.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk) | **本讲主角**。手写式的构建脚本，把 `v++ -c`/`-l`/`-p` 三段命令明明白白列出来，最适合教学阅读。 |
| [dsp/L2/examples/vss_fft_ifft_1d/Makefile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile) | 由 makefile-generator 生成的「标准模板」Makefile，负责平台查找、host 编译、target 分派。 |
| [dsp/L2/examples/vss_fft_ifft_1d/utils.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk) | 工具链辅助：平台搜索、`HOST_ARCH` 判定、`LINK_TARGET_FMT`（决定链接产物是 xsa 还是 xclbin）。 |
| [dsp/L2/examples/vss_fft_ifft_1d/system.cfg](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg) | **连接配置文件**。声明内核实例、存储端口、PL↔AIE 流连接、时钟频率、Vivado 实现选项。 |
| [dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk) | VSS（Versal Signal Stream）子流程的 makefile，生成 AIE 图容器 `libadf.a` 与 `.vss` 中间件，是链接阶段的输入之一。 |

一句话理清它们的调用关系：`Makefile`（标准模板）通过 `pre_build` 目标调用 `example.mk`（手写构建逻辑），而 `example.mk` 的 `vss:` 目标又调用 `vss_fft_ifft_1d.mk` 生成 AIE 侧产物。

---

## 4. 核心概念与源码讲解

### 4.1 三段流程总览：XO → xsa → xclbin

#### 4.1.1 概念说明

L2 构建遵循一条严格的三段流水线，每一段的输入是上一段的输出：

```
                         v++ -c (compile)            v++ -l (link)              v++ --package (package)
C++ 内核源码   ───────────────────────────▶   XO   ──────────────────────▶   xsa   ──────────────────────▶  xclbin
(.cpp/.hpp)                                       (.xo)                       (.xsa)                        (+ SD 卡)
```

- **第 1 段 编译（`-c`）**：每个内核（PL 的 `mm2s`/`s2mm`，或 AIE 图）单独编译，得到各自的中间产物。PL 内核 → `.xo`；AIE 图 → `libadf.a`。
- **第 2 段 链接（`-l`）**：把所有 `.xo`（加上 AIE 容器）按 `system.cfg` 的描述拼接成一个整体硬件容器 `.xsa`。链接阶段决定了**谁连到谁**。
- **第 3 段 打包（`--package`/`-p`）**：把 `.xsa`（+ `libadf.a`）封装成主机可加载的 `.xclbin`；对嵌入式平台（Versal/Zynq），还顺带生成一整张 SD 卡（`Image`/`rootfs`/启动脚本）。

> 为什么非要拆成三段，而不像 `gcc a.cpp -o app` 那样一步到位？因为硬件系统太重：编译一个内核可能几分钟、链接可能几十分钟、`hw` 打包可能几小时。拆段后可以**缓存中间产物**——改一个内核只需重编那一个 `.xo`，其余 `.xo` 复用，再链接一次即可，不必从头来。这正是「编译-链接」分离在硬件世界里的价值。

#### 4.1.2 核心流程

以 `vss_fft_ifft_1d` 为例，`example.mk` 里的 `all` 目标把整条流水线串起来：

```text
make all
  ├── vss            (调用 vss_fft_ifft_1d.mk，生成 libadf.a + 各 PL 转置 XO + .vss)
  ├── example_xclbin (v++ -c 编译 mm2s/s2mm 为 XO；v++ -l 链接成 kernel_pkg.xsa)
  ├── example_host   (交叉编译 host.cpp 为 host.elf)
  ├── example_sd_card(v++ --package 生成 kernel.xclbin + SD 卡)
  └── example_run    (启动 hw_emu 跑 host.elf，grep PASS)
```

这条规则定义在 [dsp/L2/examples/vss_fft_ifft_1d/example.mk:49](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L49)：

```makefile
all: vss example_xclbin example_host example_sd_card example_run
```

#### 4.1.3 源码精读

注意 `vss` 目标必须最先跑，因为它产出后续链接需要的 `.vss` 与 `libadf.a`。它在 [example.mk:27-29](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L27-L29) 里委托给库内的 `vss_fft_ifft_1d.mk`：

```makefile
vss:
	make -f ${DSPLIB_ROOT_DIR}/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk meta_check HELPER_CUR_DIR=./ HELPER_ROOT_DIR=${DSPLIB_ROOT_DIR} PARAMS_CFG=my_params.cfg
	make -f ${DSPLIB_ROOT_DIR}/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk clean vss HELPER_CUR_DIR=./ HELPER_ROOT_DIR=${DSPLIB_ROOT_DIR} PARAMS_CFG=my_params.cfg
```

`PARAMS_CFG=my_params.cfg` 是参数入口（详见 4.4 节），它驱动 AIE 图与 PL 转置内核的代码生成。`.vss` 是 Versal 专有的「PL+AIE 系统中间描述」，由 `v++ --link --mode vss` 产生，定义在 [vss_fft_ifft_1d.mk:205-206](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L205-L206)：

```makefile
${VSSFILE}: $(VSS_DEPS)
	v++ --link --mode vss --part $(PART) --save-temps  --out_dir ${OUTPUT_DIR}/${VSS} --config $(VSS_DEPS)
```

> 这一步是 Versal/AIE 路线特有的「预链接」。如果你做的是纯 PL（Alveo）内核，没有 AIE 图，就不需要 `.vss`，直接把各 `.xo` 交给 `v++ -l` 即可。本讲为完整起见保留它，但重点仍在标准的 `-c`/`-l`/`-p` 三段。

#### 4.1.4 代码实践

**实践目标**：不看答案，先在 `example.mk` 里找到三段 `v++` 命令的位置，建立「文件结构 ↔ 流程阶段」的直觉。

**操作步骤**：

1. 打开 `dsp/L2/examples/vss_fft_ifft_1d/example.mk`。
2. 搜索字符串 `v++ -c`、`v++ -l`、`v++ `（出现在 `example_sd_card` 里的 `-p`）。
3. 记录每条命令所在的目标名（`example_xclbin` / `example_sd_card`）。

**需要观察的现象**：你会发现 `v++ -c` 出现两次（`s2mm_wrapper` 与 `mm2s_wrapper` 各一条），`v++ -l` 出现一次，`v++ ... -p` 出现一次。

**预期结果**：编译段有两条 `-c`（两个 PL 数据搬运内核各编一次），印证「每个内核单独编译」；链接段一条 `-l` 把它们和 `.vss` 合并；打包段一条 `-p` 出最终产物。

---

### 4.2 第一段：`v++ -c` 编译内核为 XO

#### 4.2.1 概念说明

`v++ -c`（compile）把**一个内核的 C++ 源码**编译成一个 **XO（Xilinx Object）**文件。XO 是单个内核的硬件封装，里面包含该内核的 RTL、接口（AXI Stream / AXI Master 端口）和元数据。

关键约定：**一个 `.cpp` 对应一个 `.xo`，一个 `-k` 指定顶层内核函数名**。这和编译普通 `.o` 的直觉一致——一次编译一个翻译单元。

#### 4.2.2 核心流程

`v++ -c` 的典型参数骨架：

```text
v++ -c \
    -D<宏>=<值> ...          # 传给内核的编译期宏（如 NSTREAM、POINT_SIZE）
    -t <target>              # hw_emu / hw，决定编译产物面向仿真还是真硬件
    --platform <平台>         # 目标平台（xpfm），决定资源约束
    --save-temps             # 保留中间产物，便于调试
    -I <include 路径>         # 头件搜索路径
    -k <内核顶层函数名>        # 编译哪个函数为内核
    -o <输出 .xo>            # 输出 XO 文件名
    <输入 .cpp>              # 内核源码
```

本例中 `example_xclbin` 目标编译了两个 PL 数据搬运内核。

#### 4.2.3 源码精读

`s2mm`（stream-to-memory-mapped，把 AXI Stream 收下来写进 DDR）内核的编译命令在 [example.mk:32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L32)：

```makefile
v++ -c -DNSTREAM=$(SSR) -DPOINT_SIZE=$(POINT_SIZE) -DNITER=$(NITER) -DDATAWIDTH=$(DATAWIDTH) \
    -DPOINT_SIZE_D1=$(POINT_SIZE_D1) -DDUAL_STREAMS=$(DUAL_STREAMS) \
    -t hw_emu --platform ${PLATFORM} --save-temps \
    -I ${DSPLIB_ROOT_DIR}//xf_dsp/L1/include/hw \
    -k s2mm_wrapper -o s2mm_wrapper.xo ${DSPLIB_ROOT_DIR}/L1/tests/hw/s2mm/s2mm.cpp
```

逐项解读：

- `-DNSTREAM=$(SSR)` 等：把 `my_params.cfg` 里读出的参数（SSR=4、POINT_SIZE=4096…）以宏形式注入内核源码，让同一个 `.cpp` 能编出不同配置的内核。`SSR`（streaming split-radix）决定并行流数。
- `-t hw_emu`：本段编译产物面向**硬件仿真**（详见 4.5 节）。
- `--platform ${PLATFORM}`：目标平台，由 `utils.mk` 解析为具体 `.xpfm`（u2-l1 讲过平台查找）。
- `-k s2mm_wrapper`：顶层内核函数名，必须与 `system.cfg` 里 `nk=` 声明的名字一致。
- `-o s2mm_wrapper.xo`：输出 XO。
- 末尾的 `s2mm.cpp`：内核源码（位于 L1 测试目录，是搬数据内核的 DUT 封装）。

紧接着 [example.mk:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L33) 用几乎相同的命令编译 `mm2s`（memory-mapped-to-stream，把 DDR 数据读出变 AXI Stream）为 `mm2s_wrapper.xo`。

> 旁支：AIE 图的「编译」也是 `v++ -c`，但带 `--mode aie`，产物是 `libadf.a` 而非 `.xo`。见 [vss_fft_ifft_1d.mk:273-279](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L273-L279)。所以「编译段」在这个混合系统里其实产出两类中间件：PL 的 `.xo` 与 AIE 的 `libadf.a`。

#### 4.2.4 代码实践

**实践目标**：理解 `-D` 宏如何把参数注入内核，并验证两个数据搬运内核的源码确实存在。

**操作步骤**：

1. 打开 `dsp/L1/tests/hw/s2mm/s2mm.cpp` 与 `dsp/L1/tests/hw/mm2s/mm2s.cpp`，在文件头部找到 `NSTREAM`、`NITER`、`DATAWIDTH` 等宏的使用处。
2. 对照 `example.mk:19-25` 的参数默认值（`POINT_SIZE := 4096`、`SSR := 4`、`NITER := 4`、`DATAWIDTH := 64`），推断编译时这些宏被替换成什么。
3. 思考：如果把 `SSR` 改成 8，`-DNSTREAM=8` 会让 `s2mm.cpp` 里的 `NSTREAM` 变成几路并行流？

**需要观察的现象**：`s2mm.cpp` / `mm2s.cpp` 里会有形如 `TT_STREAM sig_i[NSTREAM_INT]` 的数组，`NSTREAM_INT` 派生自 `NSTREAM`，决定收/发多少条 AXI Stream。

**预期结果**：`SSR=4` 对应 4 路并行流（与 `system.cfg` 里 4 条 `sc = mm2s.sig_o_0..3` 一一对应）；改 `SSR=8` 则需要 8 条流，`system.cfg` 也得相应改。**待本地验证**：实际跑 `v++ -c` 需要 Vitis 工具链与 Versal 平台。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mm2s` 和 `s2mm` 要分成两条 `v++ -c` 命令，而不是一条编两个？

**参考答案**：一条 `v++ -c -k` 只能指定一个顶层内核、产出一个 XO。两个内核是两个翻译单元、两套接口，必须各编一次。这也让它们可以独立缓存——改 `mm2s` 不必重编 `s2mm`。

**练习 2**：`-k s2mm_wrapper` 里的 `s2mm_wrapper` 必须和哪个文件的哪个字段对齐？

**参考答案**：必须和 `system.cfg` 里 `nk = s2mm_wrapper:1:s2mm` 的内核名（`s2mm_wrapper`）一致，也和源码里 `extern "C"` 的顶层函数名一致。三处名字不符会导致链接阶段找不到内核。

---

### 4.3 第二段：`v++ -l` 链接为 xsa

#### 4.3.1 概念说明

`v++ -l`（link）把多个 XO（以及 AIE 容器）**按照 `system.cfg` 描述的拓扑**拼接成一个硬件容器。如果说编译是在「造零件」，链接就是在「装配」——决定哪个内核的哪个端口连到哪个存储 bank、哪个流接到哪个流。

链接的产物在 Versal/AIE 平台是 `.xsa`，在纯 PL（Alveo）平台可以直接是 `.xclbin`。这个分支由 `utils.mk` 自动判定。

#### 4.3.2 核心流程

`v++ -l` 的参数骨架：

```text
v++ -l \
    -t <target>              # hw_emu / hw
    --platform <平台>         # 目标平台
    --config <system.cfg>    # ★ 关键：连接配置，声明实例/端口/流连接
    -o <输出 .xsa>           # 链接产物
    <输入 XO 列表>           # 各个 .xo，以及（Versal 时）.vss
```

`--config` 指向的 `system.cfg` 是链接阶段的「灵魂」——4.4 节专门讲它。

#### 4.3.3 源码精读

链接命令在 [example.mk:34](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L34)：

```makefile
v++ -l -g -t hw_emu --platform ${PLATFORM} --config system.cfg \
    -o kernel_pkg.xsa mm2s_wrapper.xo s2mm_wrapper.xo vss_fft_ifft_1d/vss_fft_ifft_1d.vss
```

解读：

- `-l`：链接模式。
- `-g`：保留调试信息（生成 debug IP，便于 `hw_emu` 时抓波形）。
- `--config system.cfg`：把连接关系交给链接器（4.4 节细看）。
- `-o kernel_pkg.xsa`：产物是 `kernel_pkg.xsa`。
- 末尾三个输入：两个 PL 内核 XO（`mm2s_wrapper.xo`、`s2mm_wrapper.xo`）+ 一个 Versal 系统中间件 `vss_fft_ifft_1d.vss`（它内部已经包含了 AIE 图 `libadf.a` 与各 PL 转置内核）。

**产物格式分支**：链接产物到底是 `.xsa` 还是 `.xclbin`，由 [utils.mk:167-171](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/utils.mk#L167-L171) 决定：

```makefile
# 1) for aie flow from 2022.1
ifeq (on, $(IS_VERSAL))
LINK_TARGET_FMT := xsa
else
LINK_TARGET_FMT := xclbin
endif
```

`IS_VERSAL` 由 `utils.mk` 用 `platforminfo` 查目标平台架构得到（u2-l1 讲过 `platforminfo`）。Versal（含 AIE）走 `.xsa`，因为还需要后续打包步骤把 AIE 固件、PDI 等一起封进 SD 卡；纯 PL 的 Alveo 卡则可直接链接成 `.xclbin`，主机加载即用。

> 这就解释了为什么本例产物叫 `kernel_pkg.xsa` 而不是 `kernel.xclbin`——它是「半成品」，还要进第三段打包。

#### 4.3.4 代码实践

**实践目标**：弄清链接阶段「输入是谁、配置是谁、产物是谁」三件事。

**操作步骤**：

1. 在 `example.mk:34` 的链接命令里，圈出三个输入文件（`mm2s_wrapper.xo`、`s2mm_wrapper.xo`、`.vss`）。
2. 问自己：如果删掉 `--config system.cfg`，链接器还能知道 `mm2s` 该连到哪个内存 bank 吗？
3. 打开 `utils.mk` 第 167-171 行，确认 Versal → `xsa`、非 Versal → `xclbin` 的分支。

**需要观察的现象**：去掉 `--config` 后，链接器会报错或按默认连法（通常不符合预期），因为存储端口（`sp`）与流连接（`sc`）全部丢失。

**预期结果**：`system.cfg` 是链接的必选输入；产物 `kernel_pkg.xsa` 是一个还差最后打包的中间容器。

#### 4.3.5 小练习与答案

**练习 1**：链接产物 `kernel_pkg.xsa` 能直接被主机 `xrt::device::load_xclbin` 加载吗？

**参考答案**：不能。`.xsa` 是中间硬件容器，还需第三段 `v++ --package` 封装成 `.xclbin` 才能加载。（纯 PL 路线链接直接出 `.xclbin` 的例外情况，见上文 `LINK_TARGET_FMT` 分支。）

**练习 2**：为什么 Versal 平台链接出 `.xsa` 而非 `.xclbin`？

**参考答案**：Versal/AIE 系统除了 PL RTL，还包含 AIE 固件、PDI（可编程设备镜像）、嵌入式启动镜像等，需要 `--package` 阶段把它们整体封装进 SD 卡，故链接先产出中间的 `.xsa`。

---

### 4.4 `system.cfg` 连接配置

#### 4.4.1 概念说明

`system.cfg`（也叫 connectivity 配置 / ini 配置）是链接阶段最重要的输入文件。它用纯文本回答三个问题：

1. **实例化（`nk`）**：每个内核要实例化几个、叫什么名字？
2. **存储端口（`sp`，streaming port / slave port）**：内核的 AXI Master 端口挂到哪个 DDR/HBM bank？
3. **流连接（`sc`，stream connection）**：哪个内核的哪个 AXI Stream 输出接到哪个内核的哪个输入？

此外还能配置时钟频率（`freqhz`）和 Vivado 实现选项（`[vivado]` 段）。

#### 4.4.2 核心流程

`system.cfg` 的语法分段（section）+ 键值对：

```ini
[clock]                  # 或写成顶层 freqhz=
freqhz=<频率>:<内核>.ap_clk,...

[connectivity]
nk = <内核函数>:<实例数>:<实例名>     # 实例化
sp = <实例名>.<端口>:<bank>            # 存储端口绑 bank
sc = <实例名>.<输出流>:<实例名>.<输入流> # 流到流的连接

[vivado]                # 可选：Vivado 实现参数
prop = run.impl_1.steps.<步骤>.is_enabled=1
```

记住三个缩写：**`nk`（kernel 实例）、`sp`（port 绑 bank）、`sc`（stream 连接）**。

#### 4.4.3 源码精读

本例的 `system.cfg` 完整描述了「PL 数据搬运器 ↔ AIE 图」的连接。

**① 时钟频率**（[system.cfg:1-2](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L1-L2)）：

```ini
freqhz=312500000:vss_fft_ifft_1d_front_transpose.ap_clk,vss_fft_ifft_1d_transpose.ap_clk,vss_fft_ifft_1d_back_transpose.ap_clk,mm2s.ap_clk,s2mm.ap_clk
```

把 5 个内核（3 个 PL 转置 + mm2s + s2mm）的 `ap_clk` 都设为 312.5 MHz。时钟在链接期绑定，因为不同时钟域之间可能需要插入同步逻辑。

**② 内核实例化**（[system.cfg:10-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L10-L11)）：

```ini
nk = mm2s_wrapper:1:mm2s
nk = s2mm_wrapper:1:s2mm
```

格式 `函数名:实例数:实例名`。这里 `mm2s_wrapper`（编译时的 `-k` 名）实例化 1 份，实例名叫 `mm2s`。后续 `sp`/`sc` 都用这个**实例名**（不是函数名）来引用。这正好呼应 u4-2 讲过的「主机取 kernel 时用 `函数名:{实例名}`，实例名要和 `system.cfg` 的 `nk` 对齐」。

**③ 存储端口绑 bank**（[system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14) 与 [system.cfg:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33)）：

```ini
sp=mm2s.mem:LPDDR
...
sp=s2mm.mem:LPDDR
```

把 `mm2s` 和 `s2mm` 的 AXI Master 端口（`.mem`）都绑到 `LPDDR`。这就是 u4-3 讲的 `group_id` 的源头——主机建 `xrt::bo` 时挂的 bank，最终由这里的 `sp` 在链接期决定。本例两个搬运器同绑一个 `LPDDR`，是「单 bank 反例」（带宽不叠加）；多 bank 分区能并行提速，详见 u12-2。

**④ 流连接（PL↔AIE 边界）**（[system.cfg:23-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L23-L31)）：

```ini
# connect mm2s
sc = mm2s.sig_o_0:vss_fft_ifft_1d_front_transpose.sig_i_0
sc = mm2s.sig_o_1:vss_fft_ifft_1d_front_transpose.sig_i_1
sc = mm2s.sig_o_2:vss_fft_ifft_1d_front_transpose.sig_i_2
sc = mm2s.sig_o_3:vss_fft_ifft_1d_front_transpose.sig_i_3
# connect s2mm
sc = vss_fft_ifft_1d_back_transpose.sig_o_0:s2mm.sig_i_0
sc = vss_fft_ifft_1d_back_transpose.sig_o_1:s2mm.sig_i_1
sc = vss_fft_ifft_1d_back_transpose.sig_o_2:s2mm.sig_i_2
sc = vss_fft_ifft_1d_back_transpose.sig_o_3:s2mm.sig_i_3
```

格式 `源实例.源流:目的实例.目的流`。这 8 条 `sc` 描述了数据走向：

- **前向**：`mm2s` 把 DDR 数据读出，经 4 条流（`sig_o_0..3`）灌进 AIE 图的入口转置内核（`front_transpose.sig_i_0..3`）；
- **后向**：AIE 图出口转置内核（`back_transpose.sig_o_0..3`）把结果经 4 条流交给 `s2mm`，写回 DDR。

这 4 条流恰好对应 `SSR=4`（4 路并行），与编译时 `-DNSTREAM=$(SSR)` 一致。改 SSR 必须同步改这里的 `sc` 数量——这正是 VSS 子流程用 Python 脚本 `vss_fft_ifft_1d_con_gen.py` 自动生成 `system.cfg` 的原因（见 [vss_fft_ifft_1d.mk:202-203](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d.mk#L202-L203)），避免手写易错。

**⑤ Vivado 实现选项**（[system.cfg:39-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L39-L46)）：

```ini
[vivado]
prop=run.impl_1.steps.phys_opt_design.is_enabled=1
prop=run.impl_1.steps.post_route_phys_opt_design.is_enabled=1
param=project.enableUnifiedAIEFlow=true
```

开启布局后物理优化（`phys_opt_design`）以改善时序，并启用统一 AIE 流程让 Vivado 报告里能看到 AIE 资源。这些只在 `hw` 真综合实现时生效。

#### 4.4.4 代码实践

**实践目标**：在 `system.cfg` 里追踪一条完整的数据通路：DDR → mm2s → AIE → s2mm → DDR。

**操作步骤**：

1. 打开 `system.cfg`，找到 `sp=mm2s.mem:LPDDR`（数据从哪片 DDR 来）。
2. 沿 `sc = mm2s.sig_o_0:vss_fft_ifft_1d_front_transpose.sig_i_0` 跟到 AIE 入口。
3. 假设 AIE 内部处理完（这部分连接在 `.vss` 里），从 `sc = vss_fft_ifft_1d_back_transpose.sig_o_0:s2mm.sig_i_0` 跟到 `s2mm`。
4. 到 `sp=s2mm.mem:LPDDR`（结果写回哪片 DDR）。

**需要观察的现象**：数据走了 `LPDDR → mm2s → front_transpose → (AIE) → back_transpose → s2mm → LPDDR` 一个来回，`mm2s` 与 `s2mm` 共享同一片 `LPDDR`。

**预期结果**：你能用一句话说清每个 `nk`/`sp`/`sc` 各管什么，并理解为什么 `system.cfg` 必须在**链接阶段**（而非编译阶段）提供——因为单个内核编译时还不知道自己会和谁相连。

#### 4.4.5 小练习与答案

**练习 1**：`nk = mm2s_wrapper:1:mm2s` 里出现了三个 token，分别是什么？为什么主机代码里用 `mm2s` 而不是 `mm2s_wrapper`？

**参考答案**：分别是「内核函数名（`-k` 指定）」「实例数 1」「实例名」。主机 `xrt::kernel` 用 `函数名:{实例名}` 即 `mm2s_wrapper:{mm2s}` 取内核（u4-2），实例名用于在 `sp`/`sc` 里引用，故主机侧也用实例名。

**练习 2**：如果把 `SSR` 从 4 改成 2，`system.cfg` 里哪一部分必须改？

**参考答案**：流连接 `sc` 的数量要减半（`sig_o_0..1` 与 `sig_i_0..1`），因为并行流数随 SSR 变。这正是为什么要用脚本自动生成而非手写。

---

### 4.5 第三段：`v++ --package` 打包为 xclbin 与 SD 卡

#### 4.5.1 概念说明

`v++ --package`（简写 `-p`）是最后一段。它把链接产物 `.xsa`（纯 PL 时是链接出的 `.xclbin`）封装成**主机可加载的最终产物**：

- 对 **x86 PCIe 平台**（Alveo 卡）：产出 `.xclbin`，主机 `load_xclbin` 加载即可。
- 对 **嵌入式平台**（Versal/Zynq）：产出整张 **SD 卡**，包含 `Image`（内核）、`rootfs.ext4`（根文件系统）、启动脚本、`xclbin`/`pdi` 等，拷到 SD 卡上板启动。

#### 4.5.2 核心流程

`v++ --package` 的参数骨架：

```text
v++ -p \
    -t <target>              # hw_emu / hw
    --platform <平台>         # 目标平台
    -o <输出 xclbin>         # 最终 xclbin 名
    <输入 xsa>               # 链接产物
    <libadf.a>               # AIE 容器（Versal 时）
    --package.out_dir <目录>  # 输出目录（SD 卡内容）
    --package.rootfs <rootfs> # 嵌入式：根文件系统
    --package.kernel_image <Image> # 嵌入式：内核镜像
    --package.generate_sdcard       # 生成整张 SD 卡
    --package.boot_mode sd          # 启动模式
    --package.sd_file <文件> ...    # 额外拷进 SD 卡的文件
```

`--package.xxx` 是一组「打包指令」，控制 SD 卡里放什么、怎么启动。

#### 4.5.3 源码精读

打包命令在 [example.mk:40-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L40-L42)：

```makefile
example_sd_card:
	emconfigutil --platform ${PLATFORM} --od ./
	v++ -t hw_emu --platform ${PLATFORM} -o kernel.xclbin -p kernel_pkg.xsa libadf.a \
	    --package.defer_aie_run --package.out_dir package_hw_emu \
	    --package.rootfs ${SYSROOT}/../../rootfs.ext4 --package.generate_sdcard \
	    --package.kernel_image ${SYSROOT}/../../Image --package.boot_mode sd \
	    --package.sd_file run_script.sh --package.sd_file host.elf \
	    --package.sd_file emconfig.json --package.sd_file data/input_front.txt \
	    --package.sd_file data/ref_output.txt
```

逐项解读：

- `emconfigutil`：先生成 `emconfig.json`，描述仿真平台的设备拓扑（`hw_emu` 必需）。
- `-p kernel_pkg.xsa libadf.a`：输入是上一步的 `xsa` + AIE 容器 `libadf.a`。
- `-o kernel.xclbin`：最终 xclbin。
- `--package.defer_aie_run`：AIE 图不在加载时立即启动，而由主机程序显式 `xrt::graph::run` 控制（u13-2 讲）。
- `--package.out_dir package_hw_emu`：SD 卡内容输出到 `package_hw_emu/` 目录。
- `--package.rootfs ... / --package.kernel_image ...`：嵌入式的根文件系统与内核镜像（来自 `SYSROOT`，u2-l1 提过嵌入式需 `SYSROOT`）。
- `--package.generate_sdcard --package.boot_mode sd`：生成 SD 卡镜像，SD 启动模式。
- `--package.sd_file ...`：把 `run_script.sh`、`host.elf`、输入数据 `input_front.txt`、参考输出 `ref_output.txt` 一并拷进 SD 卡。

打包之后，`example_run`（[example.mk:44-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L44-L46)）启动 hw_emu 跑起来：

```makefile
example_run:
	./package_hw_emu/launch_hw_emu.sh -no-reboot -run-app run_script.sh
	grep "TEST PASSED, RC=0" ./package_hw_emu/qemu_output.log || exit 1
```

`launch_hw_emu.sh` 是 `--package` 顺带生成的 QEMU 启动脚本，它在 ARM 仿真器里跑起整个嵌入式 Linux，执行 `run_script.sh`（u2-l1 讲过 `SYSROOT`，这里就是用它的 Linux）。`run_script.sh`（[run_script.sh:17-32](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/run_script.sh#L17-L32)）设好环境变量后执行 `./host.elf`，并以 `echo "INFO: TEST PASSED, RC=0"` 报结果——这就是上面 `grep` 判 PASS 的依据。

#### 4.5.4 代码实践

**实践目标**：弄清 `--package` 为嵌入式平台生成了哪些文件，以及它们如何协同启动。

**操作步骤**：

1. 在 `example.mk:42` 的命令里，数一数有几个 `--package.sd_file`，列出它们各自的用途（启动脚本 / 主机程序 / 输入数据 / 参考答案）。
2. 打开 `run_script.sh`，找到它如何设置 `XCL_EMULATION_MODE=hw_emu` 与执行 `./host.elf`。
3. 思考：为什么 `input_front.txt` 和 `ref_output.txt` 要被 `--package.sd_file` 拷进 SD 卡？

**需要观察的现象**：SD 卡里既有可执行程序（`host.elf`）、又有输入与参考数据、还有启动脚本，缺任何一个都会让板上跑不起来或无法判 PASS。

**预期结果**：`--package` 阶段把「程序 + 数据 + 启动逻辑」打包成一个自洽的 SD 卡目录。**待本地验证**：实际生成 SD 卡需要 `SYSROOT`、`Image`、`rootfs.ext4`（嵌入式 Common Image），见 u15-2。

#### 4.5.5 小练习与答案

**练习 1**：x86 Alveo 平台和 Versal 嵌入式平台，`--package` 的产物有何不同？

**参考答案**：x86 Alveo 只需 `.xclbin`（主机 PCIe 加载）；Versal 嵌入式需要整张 SD 卡（`xclbin`/`pdi` + `Image` + `rootfs` + 启动脚本），因为板上是独立 Linux 系统。

**练习 2**：`--package.defer_aie_run` 的作用是什么？如果不加会怎样？

**参考答案**：它让 AIE 图在 xclbin 加载后不自动启动，改由主机程序用 `xrt::graph::run()` 显式触发。不加的话图会在加载时自动运行，主机失去对启动时机的控制，不利于精确的输入灌入与结果回收。

---

### 4.6 三种 target：sw_emu / hw_emu / hw（及 sw_emu 的现状）

#### 4.6.1 概念说明

`-t`（target）参数贯穿三段流程，决定每一步产物面向哪个「保真度层级」。三档保真度与代价逐级递增：

| target | 全称 | 做什么 | 速度 | 真实度 | 典型用途 |
|--------|------|--------|------|--------|----------|
| `sw_emu` | software emulation | 主机 C++ 直接跑，内核不综合 | 秒级 | 最低（只验算法逻辑） | 快速功能调试 |
| `hw_emu` | hardware emulation | 内核综合成 RTL，在仿真器里跑 | 分钟~小时级 | 中（RTL 级波形） | 验证硬件行为、抓接口时序 |
| `hw` | hardware | 真综合实现，上真板跑 | 编译数小时，运行秒级 | 最高 | 最终交付、性能评测 |

直觉：`sw_emu` 像「解释执行」，`hw_emu` 像「软件模拟的 CPU」，`hw` 是「真机」。

#### 4.6.2 核心流程与一个重要现状

`-t` 在三段流程里必须**保持一致**：用 `v++ -c -t hw_emu` 编的 XO，只能用 `v++ -l -t hw_emu` 链接、`v++ -p -t hw_emu` 打包。混用会报错。

> **重要现状（务必知道）**：`sw_emu` 从 Vitis **2025.1 起已被移除**。本仓库的 `Makefile` 显式拦截了它，见 [Makefile:80-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L80-L82)：
>
> ```makefile
> # add warnning for sw_emu
> ifeq ($(TARGET),sw_emu)
> $(error Error: The sw_emu target is no longer supported starting from 2025.1.)
> endif
> ```
>
> 所以虽然「三档 target」是 Vitis 的经典概念，但**当前工具版本里实际可用的是 `hw_emu` 与 `hw` 两档**。这也是为什么 `Makefile` 的默认值是 `hw_emu`（[Makefile:57](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L57)）：

```makefile
TARGET ?= hw_emu
```

`example.mk` 里所有 `v++` 命令也写死 `-t hw_emu`。开发时用 `hw_emu` 验证，交付时改成 `-t hw` 上板。

#### 4.6.3 源码精读

除了 `example.mk` 的手写命令，`Makefile`（标准模板）在嵌入式通用流程里还有一条统一的 `--package` 命令，见 [Makefile:224](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/Makefile#L224)：

```makefile
$(VPP) -t $(TARGET) --platform $(XPLATFORM) -o $(BINARY_CONTAINERS_PKG) -p $(PACKAGE_FILES) $(VPP_PACKAGE) --package.out_dir $(PACKAGE_DIR) --package.rootfs $(ROOTFS) --package.generate_sdcard --package.kernel_image $(K_IMAGE) $(SD_FILES_WITH_PREFIX) $(SD_DIRS_WITH_PREFIX)
```

这里 `-t $(TARGET)` 用变量传 target，比 `example.mk` 的写死更灵活。两条命令的骨架完全一致，差别只在「变量化 vs 字面量」——`example.mk` 是为教学/定制而手写的精简版，`Makefile` 是 makefile-generator 生成的通用版。

#### 4.6.4 代码实践

**实践目标**：确认三段流程里 `-t` 的一致性，并理解 `hw_emu` 与 `hw` 的代价差。

**操作步骤**：

1. 在 `example.mk` 里搜索所有 `-t hw_emu`，确认编译、链接、打包三段用的都是同一个 target。
2. 读 `description.json` 里的 `max_time_min`（[description.json:35-37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/description.json#L35-L37)），看 CI 给 `vitis_hw_emu` 分配了多少分钟。
3. 思考：如果改 `-t hw`，这三段哪一段代价涨得最猛？（提示：综合实现）

**需要观察的现象**：`description.json` 里 `vitis_hw_emu` 的 `max_time_min` 是 470 分钟（近 8 小时），且只声明了 `vitis_hw_emu` 一个 target——说明即使是 hw_**emu**，嵌入式 AIE 系统的耗时也已非常重。

**预期结果**：`-t hw` 会触发真实 Vivado 综合与实现（`system.cfg` 的 `[vivado]` 段才真正生效），编译时间从小时级跳到可能十几个小时；故开发迭代用 `hw_emu`，最终验证才上 `hw`。**待本地验证**：实际时间取决于平台规模与机器性能。

#### 4.6.5 小练习与答案

**练习 1**：三段流程能否混用 target（如编译用 `hw_emu`、打包用 `hw`）？为什么？

**参考答案**：不能。每段产物的元数据里记录了 target，链接/打包会校验一致性，混用报错。target 决定了内核是 RTL 仿真模型还是真综合网表，二者不兼容。

**练习 2**：既然 `sw_emu` 最快，为什么 2025.1 要移除它？

**参考答案**：`sw_emu` 只跑主机侧 C++、不经过真实编译路径，保真度太低且与现代 AIE/数据流流程脱节，容易给出「假通过」的错觉；官方改为用 `hw_emu`（带 `--package.defer_aie_run` 等）兼顾速度与真实度，故移除 `sw_emu` 以减少维护负担与误导。

---

## 5. 综合实践

**任务**：对照 `example.mk`，亲手列出从 XO 到「能上板 xclbin」的三条 `v++` 命令，并解释 `system.cfg` 在链接阶段的作用。

**具体要求**：

1. **三条命令**：从 `example.mk` 的 `example_xclbin` 与 `example_sd_card` 目标里，提炼出三段 `v++` 命令（编译、链接、打包各一条代表性的），用自己的话标注每条的「输入 → 产物」。注意编译段有两个 PL 内核，可以合并描述但要说明。
2. **`system.cfg` 的作用**：解释为什么 `system.cfg` 只在**链接**（`v++ -l`）阶段通过 `--config` 传入，而不在编译或打包阶段？它回答了哪三个问题（`nk`/`sp`/`sc`）？
3. **产物关系图**：画一张（文字版即可）从 `s2mm.cpp`/`mm2s.cpp`/AIE 源码 → `.xo` + `libadf.a` → `.vss` → `kernel_pkg.xsa` → `kernel.xclbin` + SD 卡 的流水线，标出每段的 `v++` 子命令。
4. **target 选择**：说明为什么 `example.mk` 写死 `-t hw_emu`，以及若要交付真板需要改哪里。

**参考答案要点**：

- 三条命令：① `v++ -c -k s2mm_wrapper -o s2mm_wrapper.xo ... s2mm.cpp`（mm2s 同理）；② `v++ -l --config system.cfg -o kernel_pkg.xsa mm2s_wrapper.xo s2mm_wrapper.xo vss_fft_ifft_1d/vss_fft_ifft_1d.vss`；③ `v++ -p -o kernel.xclbin kernel_pkg.xsa libadf.a --package.generate_sdcard ...`。
- `system.cfg` 只在链接传入：因为单个内核编译时还不知道会和谁相连、挂哪个 bank，只有把所有内核拼成系统时才需要「连接拓扑」；它回答实例（`nk`）、存储端口绑 bank（`sp`）、流连接（`sc`）。
- target：`hw_emu` 用于开发迭代（无需真综合），交付真板需把三段的 `-t` 统一改成 `hw`（并准备 `SYSROOT`/`Image`/`rootfs`）。

> 这是「源码阅读型实践」，不要求真跑 `v++`（需 Vitis 工具链 + Versal 平台 + Common Image）。重点是能读懂 `example.mk` 并复述流程。

---

## 6. 本讲小结

- L2 构建遵循**三段流水线**：`v++ -c`（编译，C++→XO）→ `v++ -l`（链接，XO→xsa）→ `v++ --package`/`-p`（打包，xsa→xclbin/SD 卡），每段缓存可复用。
- **XO** 是单内核封装，**xsa** 是链接后的硬件容器（Versal 专属中间产物），**xclbin** 是主机可加载的最终二进制；纯 PL 平台链接可直接出 `xclbin`，Versal/AIE 平台先出 `xsa` 再打包。
- **`system.cfg`** 是链接阶段的灵魂，回答三件事：`nk`（内核实例化）、`sp`（存储端口绑 DDR/HBM bank，即主机 `group_id` 的源头）、`sc`（AXI Stream 流连接，描述 PL↔AIE 拓扑）。
- 三档 target 保真度与代价递增：`sw_emu`（已于 2025.1 移除）< `hw_emu`（开发迭代）< `hw`（上板交付）；三段的 `-t` 必须一致。
- `example.mk` 是「手写教学版」，把三段 `v++` 命令明明白白列出来；`Makefile`（makefile-generator 生成）是「通用模板版」，用变量驱动、自动判定平台与产物格式。
- 嵌入式 Versal 系统的 `--package` 会生成**整张 SD 卡**（`xclbin`/`pdi` + `Image` + `rootfs` + 启动脚本 + 输入/参考数据），由 QEMU 启动脚本 `launch_hw_emu.sh` 拉起跑 `host.elf` 并判 PASS。

---

## 7. 下一步学习建议

- **数据搬运器深入**：本讲的 `mm2s`/`s2mm` 只是「黑盒」用了，下一讲 **u5-l2（数据搬运器与 DDR↔AIE 桥接）** 会打开它们与 `data_mover` 库的内部实现，讲清 4D 搬运器与 URAM 缓存。
- **AIE 图与 PL↔AIE 边界**：`system.cfg` 的 `sc` 描述了 PL↔AIE 连接，但要真正理解 ADF 图（`graph`/`kernel`/`connect`）与 window/stream 边界，待 **u13-l1（ADF 图、窗口/流与 PL↔AIE 边界）**。
- **存储分区与带宽**：本讲 `mm2s`/`s2mm` 同绑一个 `LPDDR`，是单 bank 反例；多 DDR/HBM 分区如何叠加带宽，见 **u12-l2（资源/时序：URAM、HBM/DDR 分区与报告）**。
- **完整部署**：`hw` 真板构建、`SYSROOT`/Common Image/SD 卡的完整准备，见 **u15-l2（完整部署：hw 构建、SD 卡与已知问题）**。
- **延伸阅读源码**：想看「变量化通用版」的三段流程，对比读 `dsp/L2/examples/vss_fft_ifft_1d/Makefile` 的 `xclbin`/`sd_card`/`run` 目标；想看 VSS 子流程如何用 Python 自动生成 `system.cfg`，读 [vss_fft_ifft_1d_con_gen.py](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_con_gen.py)。
