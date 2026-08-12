# L1/L2/L3 设计哲学与 PL/AIE 两种范式

## 1. 本讲目标

本讲是理解整个 Vitis_Libraries 仓库的「心智模型」一讲。读完本讲后，你应该能够：

1. 说清楚 **L1 / L2 / L3** 三层抽象各自要解决的问题、对应的目录约定与最终产物。
2. 区分两套构建流程：**HLS 流程**（`csim` / `csynth` / `cosim` / `vivado_syn` / `vivado_impl`）与 **Vitis 系统流程**（`sw_emu` / `hw_emu` / `hw`）。
3. 理解 **PL**（可编程逻辑 / FPGA，走 HLS→RTL）与 **AIE**（AI Engine，走 ADF 图）两条加速路线的差别、各自的目标硬件与典型库。

本讲承上启下：上一讲 [u1-l2](u1-l2-monorepo-layout.md) 讲清了「单仓库骨架与跨库配置」，本讲把视角拉近到「一个库内部如何分层、如何从源码变成上板文件」；之后的 [u2](../) 单元才会让你真正动手跑第一个用例。

---

## 2. 前置知识

本讲默认你已经读过：

- [u1-l1 项目定位与加速库全景](u1-l1-project-overview.md)：知道仓库里有 9 个活跃库，知道有 PL 与 AIE 两条加速路线。
- [u1-l2 单仓库结构与跨库配置](u1-l2-monorepo-layout.md)：知道每个库内部统一遵循 `L1/` `L2/` `L3/` 目录约定，知道 `library.json` 声明 include 路径。

几个本讲会用到的术语，先用一句话解释：

- **HLS（High Level Synthesis，高层综合）**：把 C/C++ 代码自动转换成 FPGA 上的 RTL 电路（寄存器传输级，最终是逻辑门和触发器）。Vitis 库的 PL 内核几乎都是 HLS 写的。
- **XRT（Xilinx Runtime）**：运行在主机 CPU 上的软件层，负责把任务下发到加速卡、管理缓冲、等待结果。主机程序通过 XRT（或其 OpenCL 封装）控制硬件。
- **xclbin**：一个打包好的硬件二进制（bitstream + 元数据），加速卡靠它知道「电路长什么样、有哪些内核、端口怎么连」。
- **ADF（Adaptive Data Flow）图**：AIE 专用的编程模型，用「节点（kernel）+ 边（数据连接）」描述一整张数据流图，编译器自动把它映射到 Versal 芯片的 AI Engine 阵列上。

如果你对「流（stream）」「内核（kernel）」这种词还陌生，不用急，本讲会在用到时点明。

---

## 3. 本讲源码地图

本讲主要读四个库的 README，因为它们对 L1/L2/L3 与流程的描述最权威、也最能体现「同一个分层思想在不同库里的落地差异」。

| 文件 | 作用 |
| --- | --- |
| [utils/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md) | 最经典的 L1/L2 分层定义与 HLS `TARGET` 五件套说明 |
| [dsp/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md) | 同时给出 PL（L1 HLS FFT）与 AIE（L2 AIE 图）两条路线，是 PL/AIE 对照的最佳样本 |
| [blas/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md) | 明确写出 L1/L2/L3 三层 + `sw_emu`/`hw` 两类 target，并给出 module/kernel/software-API 三级抽象 |
| [vision/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md) | 直接给出 **PL / AIE / PL+AIE** 三种内核路线，以及完整的 L1/L2/L3 目录树 |

补充：本讲在「代码实践」里还会让你实地查看 `dsp/L1`、`dsp/L2` 目录，但这些是「看目录」而不是「读某一行代码」，所以不列入上表。

---

## 4. 核心概念与源码讲解

### 4.1 L1/L2/L3 三层抽象：从原语到应用

#### 4.1.1 概念说明

Vitis 加速库的所有库都遵循同一个「三层抽象」思想。你可以把它理解成一栋楼的三层：

- **L1（原语 / 模块层）**：最底层的一个个「积木块」。一个 L1 单元通常就是一个 HLS C++ 函数（比如「把一个流复制成两路」「做一个 FFT」）。它**不关心**具体的加速卡型号、不关心 OpenCL/XRT、甚至不关心内存怎么搬——只关心「算法本身对不对、综合出来快不快、省不省资源」。L1 的典型用法是做**快速验证**：C 仿真看功能对不对，综合看资源/延迟估计。
- **L2（内核 / 系统层）**：把 L1 的积木包成一个「可以挂到系统里的内核」，并配上**主机代码**（host.cpp，用 OpenCL 或 XRT 写）。L2 关心的是「怎么把这个内核连同内存搬运一起编译成 xclbin，怎么在主机上跑起来」。
- **L3（应用 / 流水线层）**：把**多个** L2 内核串成一条流水线，解决一个完整的应用问题（比如「彩色图像 → 颜色检测 → 输出掩膜」）。L3 的产物是一个端到端的小应用。

一句话记忆：**L1 验证一个算子，L2 上板一个内核，L3 串起一条流水线。**

需要特别说明：**不是每个库三层都齐全**。比如 dsp 库目前只交付 L1/L2（没有 L3），而 blas、vision 三层都有。这种差异是正常的，分层是「约定」而非「强制齐全」。

#### 4.1.2 核心流程

三层各自回答的问题与产物可以用下面的伪流程表示：

```
L1  原语 C++  ──csim──▶ 功能对不对？
              ──csynth─▶ 资源/延迟估计（II、LUT、BRAM…）
              ──cosim──▶ 周期精确仿真
              产物：可复用的头文件 + 一份综合报告

L2  内核 + host.cpp ──v++ 编译/链接/打包──▶ xclbin + 可执行主机程序
                  产物：能上板（或仿真）的「内核 + 主机」二元组

L3  多个内核串成流水线 + 一个应用 host
                  产物：一个端到端应用（examples / benchmarks）
```

关键点：**L1 是 L2 的基础，L2 是 L3 的基础**。一个 L3 流水线里的每个内核，原则上都能在 L2 找到独立可跑的版本，而每个 L2 内核的核心算法又能在 L1 找到原始 C++ 实现。

#### 4.1.3 源码精读

四个库的 README 都复述了同一段分层定义，但措辞略有不同，对照阅读最能体会其一致性。

utils 库给出的经典定义（utils 是最底层的工具库，定义最干净）：

[utils/README.md:L23-L40](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L23-L40) —— 把 L1 定为「HLS based flow for quick checks」（功能、综合、协同仿真、导出 RTL/IP 四件事），把 L2 定为「building XCLBIN file … with host code written in OpenCL/XRT」。注意 L1 那条 Note：一旦生成了 RTL 或 XO 文件，后续才会进入 Vivado 流程生成 XCLBIN——这说明 L1 的产物可以「喂给」L2。

dsp 库的表述更精炼，并明确点出**两条路线分属不同层**：

[dsp/README.md:L3-L9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md#L3-L9) —— 「L1 level HLS C++ implementation of … FFT … for acceleration on Xilinx FPGAs」对应 PL 路线；「L2 level AIE C++ graph implementation of DDS, FFT, FIRs, Matrix Multiply (GeMM) …」对应 AIE 路线。最后一行 `Only L1/L2 primitives are delivered currently` 明确告诉我们：**dsp 库目前没有 L3**。

blas 库给出了一个独有的「三级抽象」命名，本质仍对应 L1/L2/L3：

[blas/README.md:L9-L13](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L9-L13) —— module level（= L1，C++ 实现给 HLS 用户）、kernel level（= L2，预定义内核示范如何用 L1）、software APIs level（= L3，基于 XRT 的软件 API，让纯软件工程师不写运行时就能用）。这是同一分层思想在不同受众表述下的变体。

vision 库则同时给出了 L1/L2/L3 的应用定义与 AIE-ML 的特殊流程：

[vision/README.md:L104-L129](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L104-L129) —— L1「without considering the complexities of Platform, OpenCL/XRT」，L2/L3「building XCLBIN … with host code written in OpenCL/XRT」，L3「applications developed using multiple kernels in the pipeline」。

#### 4.1.4 代码实践

**实践目标**：用源码证实「L1 是 L2/L3 的基础」这一论断，并观察三层在目录里的真实长相。

**操作步骤**：

1. 在仓库根目录，列出 `dsp/` 下的一级目录（用 `git ls-files dsp | cut -d/ -f1-2 | sort -u` 或直接看文件树）。
2. 同样列出 `blas/` 与 `vision/` 的一级目录。
3. 对照阅读 [utils/README.md:L25-L40](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L25-L40) 里对 L1/L2 的描述。

**需要观察的现象**：

- `dsp` 下有 `L1/`、`L2/`，但**没有 `L3/`**——印证 dsp README 那句「Only L1/L2 primitives are delivered currently」。
- `blas`、`vision` 下 `L1/`、`L2/`、`L3/` 三层齐全。
- 每一层内部通常都有 `examples/`、`tests/`、`include/` 这类子目录（vision 的完整目录树见 [vision/README.md:L67-L98](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L67-L98)）。

**预期结果**：你会直观看到「三层目录」是所有库的共同骨架，但「填到第几层」因库而异。

**待本地验证**：如果你的本地 checkout 有未跟踪文件，目录列表可能与上方描述略有出入；以 `git ls-files` 的结果为准。

#### 4.1.5 小练习与答案

**练习 1**：blas 库的「software APIs level」对应 L1/L2/L3 中的哪一层？为什么？

> **答案**：对应 L3。因为它「on top of the Xilinx runtime (XRT)」「allows software developers to use Vitis BLAS library without writing any runtime functions」——这是把内核封装成端到端可调用的应用接口，正是 L3「应用层」的特征。

**练习 2**：某库只有 L1 和 L2，没有 L3，这是否意味着它「不完整」或「有 bug」？

> **答案**：不是。L3 是「多内核流水线应用」，只有当某个领域确实需要把多个内核串成端到端流水线时才有意义。dsp 库目前只交付 L1/L2 原语，是因为它的定位是「提供可组合的信号处理构件」，由使用者自行组合，所以没有官方 L3 是合理的产品决策。

---

### 4.2 HLS TARGET 流程：csim → csynth → cosim → vivado

#### 4.2.1 概念说明

L1 层用 HLS 工具把 C++ 变成硬件。这个过程不是一步到位的，而是分成了**五个可独立勾选的阶段**，每个阶段回答一个不同的问题、产出不同的东西、付出不同的代价。Vitis 库把这五个阶段叫做 **TARGET**（注意大写，是 Makefile 里的变量名）。

这五个 TARGET 是：

| TARGET | 中文名 | 回答的问题 | 代价 |
| --- | --- | --- | --- |
| `csim` | C 仿真 | 算法功能对不对？（纯软件跑 C++） | 秒级，几乎无代价 |
| `csynth` | 高层综合 | 综合成 RTL 后资源/延迟估计是多少？ | 分钟级 |
| `cosim` | 协同仿真 | RTL 和软件测试台一起跑，周期是否精确？ | 分钟～小时级 |
| `vivado_syn` | Vivado 综合 | Vivado 综合后的资源/时序如何？ | 小时级 |
| `vivado_impl` | Vivado 实现 | 布局布线后的最终资源/时序如何？ | 小时级 |

理解这五个 TARGET 的关键，是明白它们是一个**由浅入深、代价递增**的阶梯：先花最少的代价（csim）确认功能，再逐步深入到更真实但更慢的阶段。

#### 4.2.2 核心流程

L1 用例的标准驱动方式是：

```
cd L1/tests/<某个用例>/
make run TARGET=<TARGET> PLATFORM=<平台>
```

`TARGET` 选其中一个，`PLATFORM` 指定目标平台（如 `u250_xdma_201830_1`，或 `.xpfm` 全路径）。所有控制变量默认值都是 `0`（即「不做」），只有你在命令行显式给出的才会执行。

阶梯式的推荐路径：

```
csim（确认功能）─▶ csynth（看资源/延迟）─▶ cosim（看周期行为）
                                              └─▶ vivado_syn ─▶ vivado_impl（看真实时序）
```

产物都落在测试工程目录下，路径名通常含 `test.prj`，综合报告（II、latency、LUT/FF/BRAM 资源估计）就在里面。

#### 4.2.3 源码精读

utils 与 blas 两个 README 给出了完全一致的 TARGET 列表：

[utils/README.md:L71-L78](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L71-L78) —— 列出五个 TARGET：`csim` / `csynth` / `cosim` / `vivado_syn` / `vivado_impl`，并各附一句解释。

[utils/README.md:L79-L94](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/README.md#L79-L94) —— 给出调用示例 `make run TARGET=csim PLATFORM=u250_xdma_201830_1`，并说明「The output files of interest can be located at … where the path name is `test.prj`」——告诉你报告去哪找。

[blas/README.md:L87-L94](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L87-L94) —— 同样的五件套，并给出带 `.xpfm` 全路径的命令行示例 `make run TARGET=<cosim/csim/csynth/vivado_syn/vivado_impl> PLATFORM=/path/to/….xpfm`。

#### 4.2.4 代码实践

**实践目标**：读懂 L1 用例的 TARGET 是怎么被 Makefile 调用的（源码阅读型，不要求真跑）。

**操作步骤**：

1. 打开任意一个 L1 用例的 Makefile，例如 `utils/L1/tests/stream_dup/Makefile`（这个用例会在 [u2-l2](../) 详细跑通）。
2. 在里面搜索 `csim`、`csynth` 这些关键字，看它们各自展开成什么命令。
3. 注意 Makefile 通常会调用 `vitis-run --mode hls`（或类似）驱动 HLS 工具，并把 `hls_config.cfg` 作为配置传入。

**需要观察的现象**：

- 五个 TARGET 并不是互斥的单选，而是「每个都有一个开关变量，默认 0」。
- 真正驱动 HLS 的底层命令是统一的，区别只在于「开哪几步」。

**预期结果**：你能用自己的话说出「`make run TARGET=csim` 实际做了什么」——即编译并运行 C++ 测试台，不做任何综合。

**待本地验证**：不同库的 Makefile 细节（变量名、底层命令名）会有差异，以你打开的那个文件为准。

#### 4.2.5 小练习与答案

**练习 1**：你改完一个 L1 内核的算法，只想先确认「功能没跑偏」，应该用哪个 TARGET？为什么不用 `vivado_impl`？

> **答案**：用 `csim`。它只跑 C++ 仿真，秒级出结果，最适合快速验证功能正确性。`vivado_impl` 要做完布局布线，耗时数小时，只用来确认「真实硬件下的时序/资源」，不适合频繁迭代的算法验证。

**练习 2**：`csynth` 和 `cosim` 都涉及 RTL，它们的本质区别是什么？

> **答案**：`csynth` 只是把 C++ **综合成** RTL 并给出资源/延迟**估计**，但不跑这个 RTL；`cosim` 则把生成的 RTL 和软件测试台**一起跑**，做**周期精确**的协同仿真，能暴露出流水线冒险、握手时序等综合阶段看不到的问题。前者快且给估计，后者慢但更接近真实行为。

---

### 4.3 Vitis 系统流程：sw_emu / hw_emu / hw

#### 4.3.1 概念说明

L2/L3 层不再只验证一个算子，而是要把「内核 + 主机程序 + 内存连接」打包成能在加速卡上跑的整个系统。这套流程由 Vitis 的 `v++` 工具驱动，最终产出 `xclbin`（硬件二进制）+ 主机可执行文件。

和 L1 的五个 TARGET 类似，L2/L3 也有三种 **target**（注意这里是小写，是 Vitis 编译选项，和 L1 的大写 TARGET 不是一回事，容易混淆，务必区分）：

| target | 含义 | 代价 | 用途 |
| --- | --- | --- | --- |
| `sw_emu` | 软件仿真 | 秒～分钟级 | 主机+内核功能联调，内核跑在 x86 上，不涉及时序 |
| `hw_emu` | 硬件仿真 | 小时级 | RTL 级仿真，跑真实硬件逻辑（含 AIE 图与 PL），但慢 |
| `hw` | 真实硬件 | **数小时**（编译到 bitstream 极慢） | 上板运行，性能数据可信 |

核心直觉：**从 `sw_emu` 到 `hw_emu` 到 `hw`，保真度越来越高，代价也越来越大**。开发时通常在 `sw_emu` 里快速调通逻辑，再用 `hw_emu` 验证硬件行为，最后才花数小时编译 `hw` 上板。

对于 AIE 路线，`hw_emu` 之前还有两个 AIE 专属的仿真阶段：**AIE x86 Functional Simulation**（快速功能检查）和 **AIE SystemC Simulation**（周期近似的性能检查）。

#### 4.3.2 核心流程

L2/L3 用例的典型命令：

```
cd L2/tests/<用例>/
make run TARGET=hw_emu PLATFORM=<平台>
# 或
make run TARGET=hw PLATFORM=<平台>
```

Vitis 用例的 Makefile 通常还支持 `host`（只编译主机程序）和 `xclbin`（只编译硬件二进制）作为独立目标，方便分步构建。

底层 `v++` 的三段流程（后续 [u5-l1](../) 会精讲）：

```
v++ -c   内核源码 ──▶ XO（内核对象）
v++ -l   XO + system.cfg ──▶ xsa（链接，描述内核如何连到内存/端口）
v++ --package  xsa + host ──▶ xclbin / SD 卡
```

#### 4.3.3 源码精读

blas README 明确列出 `hw_emu` 与 `hw` 两种 target 的含义与代价：

[blas/README.md:L121-L126](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/blas/README.md#L121-L126) —— 「`hw_emu` is for hardware emulation」「`hw` is for deployment on physical card. (Compilation to hardware binary often takes hours.)」「the Vitis case makefile also allows `host` and `xclbin` as build target」。

dsp README 给出了 AIE 路线独有的、更完整的仿真阶梯：

[dsp/README.md:L23-L30](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md#L23-L30) —— AIE x86 Functional Simulation、AIE SystemC Simulation、Software emulation、Hardware emulation（simulate the entire system, including AI Engine graph and PL logic along with XRT-based host）、Build and test on hardware——这是 AIE 系统从快到慢的完整五段式。

vision README 的 L2/L3 段也列出了 AIE-ML 的四段流程：

[vision/README.md:L121-L126](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L121-L126) —— AIE simulation、X86 simulation、Hardware emulation、Hardware build and run。

#### 4.3.4 代码实践

**实践目标**：用一个真实的 L2 用例 Makefile，确认 `TARGET=hw_emu` 与 `TARGET=hw` 走的是同一条 Makefile、只是底层 target 不同（源码阅读型）。

**操作步骤**：

1. 打开 `dsp/L2/examples/vss_fft_ifft_1d/example.mk`（这是 dsp 最完整的端到端 AIE 示例，[u6-l3](../) 会精讲）。
2. 在其中搜索 `hw_emu`、`hw`、`TARGET`，观察它是如何根据 `TARGET` 选择 `v++` 的 `--target` 选项的。
3. 再搜索 `host`、`xclbin`、`package`，确认它们是可独立调用的目标。

**需要观察的现象**：

- 同一个 Makefile 既能跑仿真也能上板，切换只靠 `TARGET` 一个变量。
- `hw` 分支会触发最耗时的 `v++ --package`，而 `hw_emu` 不会产出真实 bitstream。

**预期结果**：你能解释「为什么团队开发时几乎都用 `hw_emu`，只有 release 前才编 `hw`」——因为 `hw` 编译动辄数小时。

**待本地验证**：`example.mk` 的具体变量名与 target 名以你本地文件内容为准；不同示例的 Makefile 结构略有差异。

#### 4.3.5 小练习与答案

**练习 1**：L1 的大写 `TARGET=csim` 和 L2 的小写 `TARGET=hw_emu` 是同一回事吗？

> **答案**：不是。它们虽然都叫 `TARGET`（Makefile 变量名恰好相同），但分属两套完全不同的流程：前者是 **HLS 流程**的五阶段之一（csim/csynth/cosim/vivado_syn/vivado_impl），用来验证单个 L1 算子；后者是 **Vitis 系统流程**的三 target 之一（sw_emu/hw_emu/hw），用来构建整个「内核+主机」系统。在 L1 用例里写 `TARGET=hw_emu` 是无效的，反之亦然。

**练习 2**：为什么 AIE 路线比纯 PL 路线多了「x86 仿真」和「SystemC 仿真」两个阶段？

> **答案**：因为 AIE 图会被编译到 Versal 的 AI Engine 阵列上，这是一种和 FPGA PL 截然不同的计算资源（标量/矢量处理器核，而非自定义逻辑门）。AIE 有自己的仿真模型：x86 仿真快速验证图的功能逻辑，SystemC 仿真给出周期近似的性能估计。这两步在进入昂贵的 `hw_emu`（含 RTL）之前，提供了便宜的中间检查点。

---

### 4.4 PL 与 AIE 两种加速范式

#### 4.4.1 概念说明

整个 Vitis_Libraries 仓库里，所有的加速内核最终都落在**两种硬件资源**之一上。vision README 把这一点说得最清楚，它列出了三种内核路线，其中前两种就是本讲要对照的两大范式：

- **PL（Programmable Logic，可编程逻辑）**：即传统 FPGA 逻辑。内核用 C/C++/HDL 写，走 HLS（或直接 RTL）综合成逻辑门。PL 的优势是**极致可定制**——你可以为某个算子专门设计一套流水线，做到很高的吞吐；劣势是开发周期长、综合慢。PL 内核跑在 **Alveo 数据中心卡**（U50/U55C 等 PCIe 卡）和 **Zynq SoC** 上。
- **AIE（AI Engine）**：Versal 器件里专门的一块**AI Engine 阵列**（一组标量+矢量处理器核，带高速互连）。内核用 C/C++ 按 AIE 编程方法学编写，再用 **ADF 图**（数据流图）描述内核之间的连接，编译器自动把图映射到阵列上。AIE 的优势是**数据流友好、编程模型更接近软件**、编译快；适合 DSP、信号处理、机器学习推理。AIE 跑在 **Versal** 器件上（VCK190/VEK280/VEK385 等），**AIE-ML** 是面向机器学习增强的 AIE 变体。
- **PL+AIE**：同一个系统里同时用两种资源——典型场景是用 PL 内核（mm2s/s2mm）在 DDR 与 AIE 阵列之间搬运数据，中间的计算交给 AIE 图。dsp 的 `vss_fft_ifft_1d` 就是这种「PL 搬运 + AIE 计算」的混合系统。

一个关键认知：**同一个功能（比如 FFT）可能同时有 PL 实现和 AIE 实现两套**。选哪套，取决于你的目标硬件（是 Alveo 还是 Versal）和应用特征。

#### 4.4.2 核心流程

两种范式从源码到上板的对照：

```
PL 路线：
  C++ HLS 内核 ──HLS──▶ RTL ──v++──▶ XO ──v++ -l/package──▶ xclbin
  典型目录：L1/include/hw/*.hpp   （内核）
            L1/tests/hw/          （HLS 测试）
  典型库：utils / dsp(L1) / solver(L1) / blas / security / motor_control

AIE 路线：
  AIE 内核 C++ ──组进 ADF 图──▶ AIE 编译 ──v++──▶ xclbin/SD 卡
  典型目录：L1/include/aie/*.hpp      （AIE 内核原语）
            L2/include/aie/*_graph.hpp （把内核连成图）
            L2/tests/aie/              （AIE 系统测试）
  典型库：dsp(L2) / solver(L1 aie) / vision(aie/aie-ml)
```

PL+AIE 混合系统：PL 的 mm2s/s2mm 在「DDR 与 AIE 之间」搬数据，AIE 图做计算，二者通过 stream 端口对接（详见 [u5-l2](../) 与 [u13-l1](../)）。

#### 4.4.3 源码精读

vision README 直接给出三种内核路线的定义，是本讲最权威的一处引用：

[vision/README.md:L55-L59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L55-L59) —— `PL [HLS/RTL]`（targeting FPGA, coded in C/C++/HDL for Vitis HLS）、`AIE`（targeting AI Engine programmed in C/C++）、`PL+AIE`（target both）。

紧接着它说明了两种内核在目录里的落点：

[vision/README.md:L61-L63](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L61-L63) —— 「all unit level kernels are located in `L1/include`. AI Engine kernels are located in `L1/include/aie`」。也就是说：**PL 内核与 AIE 内核都在 L1/include 下，但 AIE 单独放在 `aie/` 子目录**——这是区分两种范式的目录信号。

dsp README 的一段话同时点出了「同功能双实现」：

[dsp/README.md:L4-L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md#L4-L7) —— L1 用 HLS C++ 实现 FFT（PL 路线），L2 用 AIE C++ 图实现 DDS/FFT/FIRs/GeMM（AIE 路线）。FFT 同时出现在两行里，正是「同一功能有 PL 和 AIE 两套实现」的直接证据。

至于目标硬件，vision README 列得最全：

[vision/README.md:L5-L7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/README.md#L5-L7) —— 「designed to work with Zynq, Zynq Ultrascale+, Versal, and Alveo FPGAs. … verified on zcu102, zcu104, vck190, U50, and U200 boards. AIE-ML functions are verified on VEK280 board.」—— Alveo（U50/U200）是 PL 路线的 PCIe 卡；Versal（vck190/VEK280）是 AIE/AIE-ML 路线的载体。

#### 4.4.4 代码实践

**实践目标**：在 dsp 库里，亲眼看到「同一功能（FFT）的 PL 与 AIE 两套实现分别放在哪个目录」。

**操作步骤**：

1. 在仓库根目录执行（任选一种）：
   - `git ls-files 'dsp/L1/include/hw/*fft*'`
   - 或用 Glob 模式 `dsp/L1/include/hw/*fft*`
2. 再执行：
   - `git ls-files 'dsp/L1/include/aie/*fft*'`
   - 或 Glob 模式 `dsp/L1/include/aie/*fft*`

**需要观察的现象**：

- `dsp/L1/include/hw/` 下有 `vitis_fft/`、`vitis_2dfft/`——这是 **PL（HLS）** FFT。
- `dsp/L1/include/aie/` 下有 `mixed_radix_fft.hpp`、`fft_ifft_dit_1ch.hpp`、`fft_window.hpp` 等——这是 **AIE** FFT 的内核原语。

**预期结果**：FFT 在 L1 层既有 PL 实现（`include/hw/`），又有 AIE 实现（`include/aie/`），二者并存。这印证了 4.4.1 的论断。

**待本地验证**：文件名以本地 `git ls-files` 输出为准；本讲写作时确认上述文件均存在。

#### 4.4.5 小练习与答案

**练习 1**：你拿到一块 Alveo U50 卡，想加速一个 FFT；又有人拿着 Versal VCK190 板，也要做 FFT。他们分别应该选 dsp 库的哪套实现？

> **答案**：Alveo U50 是 PCIe 数据中心卡，只有 PL（FPGA）资源，应选 **PL 实现**，即 `dsp/L1/include/hw/vitis_fft/`；Versal VCK190 带 AI Engine 阵列，可选用 **AIE 实现**，即 `dsp/L1/include/aie/` 下的 FFT 内核 + `dsp/L2/include/aie/` 下的图（或现成的 `vss_fft_ifft_1d` 端到端示例）。选型取决于目标硬件有没有 AIE 资源。

**练习 2**：vision README 说「AI Engine kernels are located in `L1/include/aie`」。为什么 AIE 内核要单独放一个子目录，而不是和 PL 内核混在一起？

> **答案**：因为两者的**编程模型、编译工具链、目标硬件**都完全不同。PL 内核走 HLS（Vitis HLS）综合到 FPGA 逻辑；AIE 内核走 AIE 编译器映射到 AI Engine 阵列。把它们分目录存放，既方便各自的 include 路径管理（见 [u1-l2](u1-l2-monorepo-layout.md) 的 `library.json`），也让人一眼看出「这个文件该用哪条工具链处理」。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿任务**（本讲的指定代码实践任务）。

### 任务：为 dsp 库绘制 L1/L2/L3 目录画像，并定位 FFT 的 PL/AIE 双实现

**目标**：综合运用「三层抽象」「PL/AIE 范式」两个心智模型，亲手测绘 dsp 库的内部结构。

**步骤**：

1. **列出 dsp 三层目录**。在仓库根目录执行：

   ```bash
   git ls-files dsp/L1 | cut -d/ -f3 | sort -u    # L1 下的子目录
   git ls-files dsp/L2 | cut -d/ -f3 | sort -u    # L2 下的子目录
   git ls-files dsp/L3 | cut -d/ -f3 | sort -u    # L3 下的子目录（若无输出，说明 dsp 没有 L3）
   ```

   如果你无法运行 bash，改用 Glob 工具：分别 Glob `dsp/L1/**`、`dsp/L2/**`、`dsp/L3/**` 并人工汇总第一级子目录。

2. **填写下面这张表**（预期答案见后）：

   | 层 | 存在? | 一级子目录举例 |
   | --- | --- | --- |
   | L1 | ? | |
   | L2 | ? | |
   | L3 | ? | |

3. **判断 FFT 的双实现位置**。回答两个问题：
   - FFT 的 **PL（HLS）** 实现在哪个目录？（提示：`dsp/L1/include/hw/…`）
   - FFT 的 **AIE** 实现在哪个目录？（提示：AIE 内核原语在 `dsp/L1/include/aie/…`，把它们连成系统的图在 `dsp/L2/include/aie/…`）
   - 据此判断：FFT 在哪一层**同时**有 PL 和 AIE 实现？

4. **对照 README 自检**：把你的结论与 [dsp/README.md:L3-L9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/README.md#L3-L9) 的描述对照，确认一致。

**预期结果（参考答案）**：

- dsp **L1** 存在，一级子目录含 `examples/`、`include/`、`tests/`；其中 `include/` 下再分 `hw/`（PL/HLS 内核，如 `vitis_fft`、`vitis_2dfft`）与 `aie/`（AIE 内核原语，如 `mixed_radix_fft.hpp`、`fft_ifft_dit_1ch.hpp`、`dds_mixer.hpp`、`fir_*.hpp`、`matrix_mult.hpp`、`bitonic_sort.hpp`）；`tests/` 下是 `hw/`（HLS 测试，如 `1dfft`、`2dfft`、`ssr_fft`）。
- dsp **L2** 存在，一级子目录含 `benchmarks/`、`examples/`（含 `vss_fft_ifft_1d`、`fir_129t_sym`、`docs_examples` 等）、`include/aie/`（AIE 图 `*_graph.hpp`）、`tests/aie/`（AIE 系统测试，每个内核一个目录，如 `bitonic_sort`、`conv_corr`、`mixed_radix_fft` 等）。
- dsp **L3** **不存在**（`git ls-files dsp/L3` 无输出），与 README「Only L1/L2 primitives are delivered currently」一致。
- FFT 的双实现：PL 版在 **L1 层**（`dsp/L1/include/hw/vitis_fft` 与 `vitis_2dfft`）；AIE 版的内核在 **L1 层**（`dsp/L1/include/aie/` 下的 `mixed_radix_fft.hpp` 等）、图在 **L2 层**（`dsp/L2/include/aie/`、`dsp/L2/tests/aie/`）。所以「同时有 PL 和 AIE 实现且都在 L1 层」的判断成立——**FFT 的 PL 与 AIE 内核原语都在 L1 层**。

**待本地验证**：以上子目录清单基于当前 HEAD（`629b2c979`）的 `git ls-files` 结果；若你切换到其他版本，个别目录可能增减，以本地实际输出为准。

---

## 6. 本讲小结

- 所有 Vitis 库都遵循 **L1/L2/L3** 三层抽象：L1 是可复用的算法原语（快速 HLS 验证），L2 是可上板的内核+主机，L3 是多内核流水线应用；三层是「基础递进」关系，但**不是每个库三层都齐全**（如 dsp 目前只有 L1/L2）。
- **L1 的 HLS 流程**有五个 TARGET：`csim`（功能）→ `csynth`（资源/延迟估计）→ `cosim`（周期精确）→ `vivado_syn`/`vivado_impl`（真实时序），代价递增、保真度递增。
- **L2/L3 的 Vitis 系统流程**有三个 target：`sw_emu`（软件仿真）→ `hw_emu`（RTL/AIE 硬件仿真）→ `hw`（上板，编译耗时数小时）；AIE 路线还多出 x86 与 SystemC 两段 AIE 专属仿真。
- 两条加速范式：**PL**（FPGA 逻辑，HLS→RTL，跑在 Alveo/Zynq，内核在 `L1/include` 或 `L1/include/hw`）与 **AIE**（AI Engine 阵列，ADF 图，跑在 Versal，内核在 `L1/include/aie`）；二者常以 **PL+AIE** 混合形态出现（PL 搬数据 + AIE 算计算）。
- **同一功能可能双实现**：如 dsp 的 FFT 既有 PL 版（`L1/include/hw/vitis_fft`），也有 AIE 版（`L1/include/aie/*fft*` + `L2/include/aie` 图），选型取决于目标硬件。
- L1 的大写 `TARGET`（HLS 五阶段）和 L2 的小写 `TARGET`（Vitis 三 target）**不是同一回事**，极易混淆，务必分清。

---

## 7. 下一步学习建议

本讲建立了「分层 + 双范式」的全局心智模型，但还没让你真正动手。建议接下来的学习路径：

1. **[u2-l1 搭建 Vitis/XRT 开发环境](../)**：装好工具链，把本讲提到的 `v++`、`vivado`、`vitis-run` 跑起来。
2. **[u2-l2 运行第一个 HLS L1 用例 stream_dup](../)**：亲手跑通一个 L1 用例的 `csim`，把本讲的「HLS TARGET 流程」从纸上变成真实输出。
3. **[u2-l3 HLS TARGET 流程与综合报告解读](../)**：本讲只点了五个 TARGET 的名字，那一讲会教你读懂 `csynth` 报告里的 II、latency、资源估计。
4. 之后进入 [u3 HLS 内核与流式数据模型](../)，深入 PL 内核的 `hls::stream`、pragma 等内部机制；AIE 方向则在 [u6 DSP 库](../) 深入 ADF 图。

如果想提前感受「PL+AIE 混合系统」，可以先扫一眼 `dsp/L2/examples/vss_fft_ifft_1d/` 的目录结构——它是本讲所有概念（L2 层、AIE 路线、PL 搬运+主机控制）的一个完整缩影，会在 [u6-l3](../) 精讲。
