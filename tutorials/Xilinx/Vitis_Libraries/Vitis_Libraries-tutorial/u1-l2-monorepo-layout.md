# 单仓库结构与跨库配置

## 1. 本讲目标

上一讲（u1-l1）我们建立了对 Vitis 加速库的全局认知：知道了它是什么、有哪些库、PL 与 AIE 两条加速路线分别面向什么硬件。本讲我们要把镜头从「全景」拉近到「骨架」，回答三个工程上的具体问题：

- 这个仓库的目录是怎么组织的？每个库里面都有哪些共有文件？
- 每个库是怎么通过 `library.json` 声明自己的 include 路径的？
- 9 个库之间的依赖、平台名到实际 xpfm 的映射、以及 CI 是怎么靠顶层几个配置文件串起来的？

学完本讲，你应该能够：

1. 说出单仓库（monorepo）的目录约定，以及每个库都有的「共有文件清单」。
2. 读懂任意一个库的 `library.json`，解释其中 `id`、`name`、`v++.includepaths`、`host` 段的含义，并能据此写出编译一个库所需的 `-I` 路径。
3. 用 `dependency.json` 画出 9 个库的依赖拓扑，并判断「要用 solver 需要同时引入哪些库」。
4. 解释 `platform_map.json` 如何把逻辑平台名（如 `vck190`）映射到具体的 `.xpfm`，并理解 `Jenkinsfile` 如何用一个共享流水线驱动所有库的 CI。

## 2. 前置知识

在进入源码前，先用通俗语言把几个名词讲清楚。这些概念在 u1-l1 已经部分提到，这里只补充与本讲「结构」相关的部分。

- **单仓库（monorepo）**：把多个相对独立的「子项目」放在同一个 Git 仓库里统一管理。Vitis_Libraries 就是这样一个仓库：顶层有 9 个库目录（`utils`、`data_mover`、`dsp`、`solver`、`blas`、`vision`、`security`、`motor_control`、`ultrasound`），每个目录本身就是一个可单独编译、单独测试的加速库。单仓库的好处是跨库复用（比如 `dsp` 直接 `#include` `utils` 的头件）和统一 CI。

- **include 路径**：C/C++ 编译时，`#include <xxx.hpp>` 会在一组「搜索路径」里找文件。Vitis 用 `LIB_DIR` 这个占位符代表「当前库的根目录」，每个库在自己的 `library.json` 里声明 `LIB_DIR/L1/include` 这样的路径，工具链会自动把 `LIB_DIR` 替换成实际路径。

- **xpfm**：`.xpfm` 文件是 Vitis 平台的描述文件（一个平台 = 一块硬件板子 + 它的可编程资源描述）。我们在命令行里写 `PLATFORM=vck190`，但工具链最终需要的是某个具体的 `.xpfm` 文件名，二者之间的翻译表就是 `platform_map.json`。

- **CI（持续集成）**：每次提交代码后自动跑测试的流水线。本仓库用 Jenkins，配置写在 `Jenkinsfile` 里。

- **L1/L2/L3**：贯穿所有库的三层抽象，u1-l1 已介绍。本讲只关心目录层面：大多数库内部都有 `L1/`（原语头件）、`L2/`（内核 + 示例），部分库还有 `L3/`（多内核应用流水线）。具体每一层做什么，是 u1-l3 的主题，本讲不展开。

## 3. 本讲源码地图

本讲涉及的关键文件都在仓库顶层，是「跨库共享」的配置与入口：

| 文件 | 作用 |
| --- | --- |
| [`dependency.json`](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L1-L48) | 顶层依赖图：声明每个库直接依赖哪些库 |
| [`platform_map.json`](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6) | 逻辑平台名 → 具体 `.xpfm` 文件名的映射表 |
| [`utils/library.json`](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/library.json#L1-L17) | utils 库的元数据与 include 路径声明（最简形态示例） |
| [`dsp/library.json`](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/library.json#L1-L17) | dsp 库的元数据与 include 路径声明（PL 头件目录形态） |
| [`Jenkinsfile`](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/Jenkinsfile#L1-L2) | 顶层 CI 入口：调用一个共享流水线库 |

此外，每个库目录内部都有一组「共有文件」，本讲会用 `utils`、`dsp`、`solver`、`vision` 作为真实样本来印证这套约定。

---

## 4. 核心概念与源码讲解

### 4.1 library.json 结构

#### 4.1.1 概念说明

每个库目录（如 `utils/`、`dsp/`）里都有一个 `library.json`。它是这个库的「身份证 + 编译说明书」，回答三个问题：

1. **我是谁**：`id`（机器用的短名，如 `xf_utils_hw`）和 `name`（人类可读名，如 `Vitis Utility Library`）。
2. **我做什么**：`description` 一句话定位。
3. **怎么编译我**：分两类用户给出 include 路径——
   - `v++.includepaths`：给 Vitis `v++` 编译器（也就是综合 HLS 内核、构建 xclbin 时）用的头件路径。
   - `host.compiler.includepaths`：给主机端 C/C++ 编译器（编译跑在 x86/ARM CPU 上的 host 程序）用的头件路径。

路径里的 `LIB_DIR` 是占位符，工具链会替换成该库的根目录。这样同一个 `library.json` 在不同机器、不同检出路径下都能用。

#### 4.1.2 核心流程

当工具链（或开发者）要使用某个库时，流程是：

```text
读取 <lib>/library.json
  → 取 v++.includepaths   → 加到 v++ 的 -I 列表（内核侧）
  → 取 host.compiler.includepaths → 加到 g++ 的 -I 列表（主机侧）
  → 若有 host.linker.libraries → 加到主机链接的 -l 列表
把 LIB_DIR 替换为该库实际根目录
```

最关键的认识是：**主机侧和内核侧的 include 路径是分开声明的**，因为它们的编译器不同、能看到的头件集合也不同。

#### 4.1.3 源码精读

先看最简形态的 `utils/library.json`：[utils/library.json:L1-L17](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/library.json#L1-L17)

```json
{
    "id": "xf_utils_hw",
    "name": "Vitis Utility Library",
    "description": "...",
    "v++": {
        "includepaths": [ "LIB_DIR/L1/include" ]
    },
    "host": {
        "compiler": {
            "includepaths": [ "LIB_DIR/L1/include" ]
        }
    }
}
```

- 第 2 行 `id` 是 `xf_utils_hw`，第 3 行 `name` 是全称。注意 id 用了 `xf_` 前缀，这是 Vitis 库的命名习惯（`xf` = Xilinx Framework / 加速库命名空间）。
- 第 6 行 `v++.includepaths` 只有一条 `LIB_DIR/L1/include`——utils 库的所有硬件原语头件都在 `utils/L1/include/xf_utils_hw/` 下。
- 第 11 行 `host.compiler.includepaths` 也是同一条路径，说明主机侧测试代码也要引用这些头件。
- **没有 `host.linker` 段**：utils 是纯头件库，不需要链接任何外部 `.so`。

再看 `dsp/library.json`：[dsp/library.json:L1-L17](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/library.json#L1-L17)

```json
    "v++": {
        "includepaths": [ "LIB_DIR/L1/include/hw" ]
    },
```

注意 dsp 的 include 路径多了一层 `hw`（`L1/include/hw`），因为 dsp 的 L1 头件区分了 `hw/`（PL/HLS 硬件）和 `aie/`（AI Engine）两套实现。这正好印证 u1-l1 讲的「同一功能可有两条路线」。

最后看一个「带链接依赖」的例子 `vision/library.json`，它的 `host` 段多了 `linker`：[vision/library.json:L10-L31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/vision/library.json#L10-L31)

```json
        "linker" : {
            "libraries" : [
                "opencv_videoio", "opencv_imgcodecs", "opencv_core", ...
            ]
        }
```

vision 的主机程序要读写图像文件，所以必须链接 OpenCV 的一系列库。这说明 `library.json` 不仅能声明 include 路径，也能声明链接库——它是该库「完整编译依赖」的声明。

#### 4.1.4 代码实践

**实践目标**：用 `library.json` 反推「编译某库主机程序需要哪些 `-I`」。

**操作步骤**：

1. 打开 [solver/library.json:L1-L20](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/library.json#L1-L20)。
2. 找到 `host.compiler.includepaths`，记录所有路径。
3. 把 `LIB_DIR` 替换为 `solver`。

**需要观察的现象**：solver 的主机 include 路径不止包含自己的 `L1/include`、`L2/include`，还多出一条 `LIB_DIR/ext/xcl2`。

**预期结果**：solver 的主机 `-I` 列表应是：

```
-Isolver/L1/include -Isolver/L2/include -Isolver/ext/xcl2
```

这说明 solver 的 host 程序用到了 `ext/xcl2` 这个跨库共用的 OpenCL 辅助库（u4-l1 会专门讲它）。

**待本地验证**：本实践只要求「读 JSON 并替换占位符」，无需运行；若想验证，可在检出仓库后手动展开路径与实际目录对照。

#### 4.1.5 小练习与答案

**练习 1**：`dsp/library.json` 里为什么主机侧和内核侧的 include 路径都是 `L1/include/hw`，而 utils 是 `L1/include`？

**参考答案**：因为 dsp 的 L1 头件按硬件路线分成了 `hw/`（PL/HLS）和 `aie/`（AI Engine）两个子目录，要引用 PL 实现就必须指到 `hw` 这一层；utils 不区分路线，所有头件直接放在 `L1/include` 下。

**练习 2**：`ultrasound/library.json` 只有 `id` 和 `name`，没有 `v++` / `host` 段（见 [ultrasound/library.json:L1-L5](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/library.json#L1-L5)）。这说明什么？

**参考答案**：说明 ultrasound 目前没有在 `library.json` 里静态声明 include 路径，它的构建方式更特殊（描述里写的是 "ultrasound AIE lib"，是纯 AIE 库），实际 include 路径很可能由它自己的 Makefile 直接指定，而非依赖 `library.json` 的通用机制。这也提醒我们：`library.json` 是「约定」，但不是每个库都填全。

---

### 4.2 dependency.json 依赖图

#### 4.2.1 概念说明

`dependency.json` 是整个仓库的「依赖地图」。它是一个 JSON 数组，每个元素形如 `{ "lib": "dsp", "dependsOn": ["utils", "data_mover"] }`，意思是「dsp 库直接依赖 utils 和 data_mover」。

为什么需要它？因为单仓库里库与库之间会互相 `#include`（比如 dsp 的内核会复用 utils 的流式原语、data_mover 的搬运器）。如果你只想用 solver，你得知道：solver 不仅依赖 dsp，还会通过 dsp 间接拉入 data_mover、utils。`dependency.json` 就是让工具链和人都能算出这个「传递依赖闭包」。

#### 4.2.2 核心流程

计算一个库的「完整依赖集合」是一个标准的图遍历问题：

```text
full_deps(L) =
    直接依赖(D) ∪ full_deps(D)  对 D ∈ dependency.json[L].dependsOn
```

以 solver 为例：

```text
solver
 ├── 直接依赖: utils, dsp
 dsp
 ├── 直接依赖: utils, data_mover
 data_mover
 └── 直接依赖: utils
 utils
 └── 依赖: (无)
```

所以 solver 的完整依赖闭包 = { utils, dsp, data_mover }。注意 `utils` 被多条路径指向，它是整张图的「公共根」。

#### 4.2.3 源码精读

完整文件见 [dependency.json:L1-L48](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L1-L48)。挑几条关键边来看：

data_mover 依赖 utils：[dependency.json:L6-L11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L6-L11)

```json
    {
        "lib": "data_mover",
        "dependsOn": [ "utils" ]
    },
```

dsp 同时依赖 utils 和 data_mover：[dependency.json:L12-L18](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L12-L18)

```json
    {
        "lib": "dsp",
        "dependsOn": [ "utils", "data_mover" ]
    },
```

solver 依赖 utils 和 dsp：[dependency.json:L29-L35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L29-L35)

```json
    {
        "lib": "solver",
        "dependsOn": [ "utils", "dsp" ]
    },
```

utils 自身无依赖（是根）：[dependency.json:L40-L43](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L40-L43)

把全部 9 条记录整理成一张依赖表：

| 库 | 直接依赖（dependsOn） |
| --- | --- |
| utils | （无） |
| data_mover | utils |
| dsp | utils, data_mover |
| solver | utils, dsp |
| security | utils |
| blas | （无） |
| vision | （无） |
| motor_control | （无） |
| ultrasound | （无） |

观察这张表可以得到两个重要结论：

1. **utils 是多数库的根依赖**：data_mover、dsp、solver、security 都直接或间接依赖它。
2. **并非所有库都连成一张图**：blas、vision、motor_control、ultrasound 这四个库 `dependsOn` 为空，它们是「独立岛」，可以单独使用。

#### 4.2.4 代码实践

**实践目标**：亲手画出依赖拓扑，并算出 dsp 的（直接 + 间接）依赖集合。

**操作步骤**：

1. 读 [dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L1-L48)。
2. 画 9 个节点，按 `dependsOn` 画箭头（A dependsOn B → 画 A→B）。
3. 对 dsp，列出它的直接依赖，再沿箭头继续走，列出间接依赖。

**需要观察的现象**：dsp 的箭头指向 utils 和 data_mover；data_mover 又指向 utils。

**预期结果**：

- dsp 的**直接**依赖 = { utils, data_mover }。
- dsp 的**间接**（传递）依赖：data_mover 又依赖 utils，所以 dsp 的完整依赖闭包 = { utils, data_mover }。其中 `utils` 是「公共根」——它既是 dsp 的直接依赖，又通过 data_mover 被间接到达。
- 额外结论：要用 solver，必须同时引入 { solver, dsp, data_mover, utils } 四个库（solver→dsp→data_mover→utils 这条主干链）。

**待本地验证**：纯阅读型实践，无需运行；可自己写个小脚本解析 JSON 验证闭包。

#### 4.2.5 小练习与答案

**练习 1**：如果想在自己的工程里只使用 `security` 库，至少要把哪些库的 include 路径加进来？

**参考答案**：security 直接依赖 utils，utils 无依赖。所以至少要加 `security` 和 `utils` 两个库的 include 路径。

**练习 2**：`dependency.json` 里 `dependsOn` 表达的是「直接依赖」还是「传递依赖」？为什么这点很重要？

**参考答案**：表达的是**直接依赖**。这点很重要，因为使用一个库时，只看它自己的 `dependsOn` 是不够的——必须递归地把依赖的依赖也一起拉进来（比如用 solver 不能漏掉 data_mover）。工具链通常会自动做这个闭包计算，但人读图时要注意区分「直接」和「间接」。

---

### 4.3 platform_map.json 平台映射

#### 4.3.1 概念说明

在 Vitis 命令行里，我们经常用一个**逻辑平台名**来指定目标硬件，比如 `PLATFORM=vck190`。但工具链真正需要的是一个具体的 `.xpfm` 平台描述文件（它通常藏在 Vitis 安装目录的 platform 仓库里）。`platform_map.json` 就是这两者之间的「电话簿」：逻辑名 → 实际 xpfm 名。

这种间接层有两个好处：

1. **可读性**：脚本里写 `vck190` 比写 `xilinx_vck190_base_202610_1` 清晰。
2. **可替换**：当平台版本升级（比如从某个 `202xxx` 版本升到下一个），只需要改这张映射表，所有脚本里的逻辑名不变。

#### 4.3.2 核心流程

```text
用户写 PLATFORM=vck190
  → 工具链查 platform_map.json["vck190"]
  → 得到 "xilinx_vck190_base_202610_1"
  → 在 PLATFORM_REPO_PATHS 指定的目录里找该名字的 .xpfm
  → 用它来综合 / 链接 / 打包
```

#### 4.3.3 源码精读

完整文件见 [platform_map.json:L1-L6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6)：

```json
{
    "vck190": "xilinx_vck190_base_202610_1",
    "vck190_dfx": "xilinx_vck190_base_dfx_202610_1",
    "vek280": "xilinx_vek280_base_202610_1",
    "vek385": "vek385_base"
}
```

几点重要观察：

1. **只列了 4 个平台，且都是 Versal/AIE 平台**：`vck190`、`vck190_dfx`、`vek280`、`vek385`。这印证 u1-l1 的结论——AIE 库面向 Versal（VCK190/VEK280/VEK385）。`vck190_dfx` 是 VCK190 的 DFx（动态功能交换）变体。
2. **xpfm 名里带版本号**（如 `202610`）：这些是 2025.2 周期对应的平台构建版本，版本升级时这张表会跟着更新。
3. **没有列 Alveo PL 平台**：因为 PL 平台（U50/U55C 等）通常直接用它们的 xpfm 名，不需要这层映射；这张表主要是给 Versal/AIE 流程用的。
4. **`vek385` 映射到 `vek385_base`**（没有 `xilinx_` 前缀和版本号）：说明不同平台的命名规范不完全一致，这正是需要一张映射表的原因之一。

#### 4.3.4 代码实践

**实践目标**：理解逻辑名与 xpfm 的对应，并体会「改一张表即可切换平台」。

**操作步骤**：

1. 读 [platform_map.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/platform_map.json#L1-L6)。
2. 假设你要在 VCK190 上跑一个 AIE 示例，写出命令里会出现的逻辑名，以及它被翻译成的 xpfm 名。
3. 假设 AMD 下个版本把 VCK190 的 base 平台改名为 `xilinx_vck190_base_202710_1`，思考需要改哪里。

**需要观察的现象**：命令行只用逻辑名 `vck190`，而真正查找的是 `xilinx_vck190_base_202610_1`。

**预期结果**：

- 逻辑名：`vck190`；对应 xpfm：`xilinx_vck190_base_202610_1`。
- 平台升级时，只需把 `platform_map.json` 里 `"vck190"` 的值改成新名字，所有用 `vck190` 的脚本都自动跟上，无需逐个改脚本。

**待本地验证**：本实践为阅读 + 推理型；若本地装了 Vitis，可用 `env | grep PLATFORM` 和实际 `.xpfm` 路径对照验证。

#### 4.3.5 小练习与答案

**练习 1**：`vck190` 和 `vck190_dfx` 的 xpfm 名有什么区别？DFx 大致指什么？

**参考答案**：`vck190` → `xilinx_vck190_base_202610_1`；`vck190_dfx` → `xilinx_vck190_base_dfx_202610_1`，多出 `_dfx`。DFx 指 Dynamic Function eXchange（动态功能交换），即在同一片 FPGA 上根据需要在多个「可重构分区」之间动态切换电路，常用于按时分复用硬件资源。

**练习 2**：为什么 `platform_map.json` 不把 Alveo 的 U50/U55C 也列进去？

**参考答案**：这张表的主要用途是给 Versal/AIE 流程做逻辑名→xpfm 的翻译；Alveo PL 平台在脚本里通常直接使用其 xpfm 名（由 `PLATFORM_REPO_PATHS` 直接定位），不需要额外的逻辑名间接层。因此表中只列了需要重定向的 Versal 平台。

---

### 4.4 Jenkinsfile CI 入口

#### 4.4.1 概念说明

仓库顶层有一个 `Jenkinsfile`，它是 CI（持续集成）的入口。但有意思的是：它只有两行，几乎不包含任何具体步骤。原因是它采用了 Jenkins 的「共享流水线库」（Shared Pipeline Library）模式——把所有库的构建/测试逻辑抽到一个外部库里，顶层 `Jenkinsfile` 只负责「调用」。

这种设计让 9 个库共用同一套 CI 逻辑，避免每个库各写一份冗长的流水线。

#### 4.4.2 核心流程

```text
Jenkins 触发构建（如提交代码）
  → 读取顶层 Jenkinsfile
  → @Library('pipeline-library')_   ← 加载名为 pipeline-library 的共享库
  → FullVitisLibPipeline(libname: 'Vitis_Libraries')  ← 调用其中的入口函数
  → 共享库内部：遍历每个库，跑各库的 L1/L2 测试（依赖各库自己的 Jenkinsfile / Makefile）
```

注意：每个库目录里**还有一个自己的 `Jenkinsfile`**（比如 `utils/Jenkinsfile`、`dsp/Jenkinsfile`）。顶层 `Jenkinsfile` 负责整体编排，库级 `Jenkinsfile` 负责该库特有的细节。本讲只看顶层的入口。

#### 4.4.3 源码精读

整个顶层 Jenkinsfile 只有：[Jenkinsfile:L1-L2](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/Jenkinsfile#L1-L2)

```groovy
@Library('pipeline-library')_
FullVitisLibPipeline(libname: 'Vitis_Libraries')
```

- 第 1 行 `@Library('pipeline-library')_`：`@Library` 是 Jenkins 加载共享库的注解，`'pipeline-library'` 是该共享库的名字，末尾的下划线 `_` 表示「加载但不显式 import 任何特定符号」（隐式导入）。真正的逻辑在共享库的 `vars/` 目录里。
- 第 2 行 `FullVitisLibPipeline(libname: 'Vitis_Libraries')`：调用共享库提供的 `FullVitisLibPipeline` 函数（一个 Groovy 脚本），传入当前仓库名 `Vitis_Libraries`。这个函数会负责发现 9 个库、按依赖顺序、按 L1/L2/L3 分层地跑测试。

要点：本仓库的 CI 实际逻辑**不在本仓库里**，而在名为 `pipeline-library` 的外部共享库里。我们能看到的是它的「调用点」。

#### 4.4.4 代码实践

**实践目标**：从顶层 `Jenkinsfile` 出发，理解「共享流水线 + 各库 Jenkinsfile」的两层结构。

**操作步骤**：

1. 读顶层 [Jenkinsfile](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/Jenkinsfile#L1-L2)。
2. 打开 `utils/Jenkinsfile` 与 `dsp/Jenkinsfile`，对比它们与顶层是否类似。
3. 思考：为什么要把流水线逻辑放进外部共享库，而不是直接写在顶层？

**需要观察的现象**：各库的 `Jenkinsfile` 也非常短，同样是「调用共享库函数」的形式。

**预期结果**：你会看到一套统一的「薄 Jenkinsfile + 厚共享库」模式。好处是：加一个新库时，只需复制一份相同的薄 Jenkinsfile，CI 逻辑零改动；改 CI 规则时，只改共享库一处，所有库同时生效。

**待本地验证**：本实践为源码阅读型，无需运行 Jenkins。若想深入，可在企业内部 Git 上找名为 `pipeline-library` 的共享库仓库阅读 `vars/FullVitisLibPipeline.groovy`（该仓库不在本项目中，属「待确认」）。

#### 4.4.5 小练习与答案

**练习 1**：顶层 `Jenkinsfile` 第 1 行末尾的下划线 `_` 是什么意思？去掉会怎样？

**参考答案**：下划线表示「加载共享库但不显式导入任何符号」（隐式全局导入），这是 Jenkins `@Library` 注解的语法要求。如果不加下划线，就必须在大括号里显式列出要导入的符号（如 `@Library('pipeline-library') { FullVitisLibPipeline }`），否则语法不完整。

**练习 2**：如果新增第 10 个库，CI 需要怎么改？

**参考答案**：在新库目录里放一份和其他库一样的「薄 Jenkinsfile」（调用共享库函数），并确保它的测试遵循 L1/L2/(L3) 目录约定与 `description.json` 元数据（u14-l1 会讲）。顶层 `Jenkinsfile` 和共享库通常无需改动——这正是共享流水线模式的价值。

---

## 5. 综合实践

**任务：依据 `dependency.json` 画出 9 个库的依赖拓扑，并算出关键库的依赖闭包。**

这是本讲的主干实践，把 `library.json`、`dependency.json`、目录约定三件事串起来。

**步骤**：

1. **画拓扑图**。读 [dependency.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L1-L48)，把 9 个库画成节点，按 `dependsOn` 画箭头。你会得到：
   - 一条主干链：`solver → dsp → data_mover → utils`（utils 是公共根）。
   - 一条小支线：`security → utils`。
   - 四个孤岛：`blas`、`vision`、`motor_control`、`ultrasound`（无依赖）。

2. **回答 dsp 的依赖**。按任务要求指出 dsp 间接依赖哪些库：
   - dsp 的**直接**依赖 = { utils, data_mover }（见 [dependency.json:L12-L18](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L12-L18)）。
   - 由于 data_mover 自身又依赖 utils（[dependency.json:L6-L11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L6-L11)），dsp 的**完整（直接+间接）依赖闭包** = { utils, data_mover }，其中 `utils` 是被多条路径共同指向的公共根——它既是 dsp 的直接依赖，又通过 data_mover 被间接到达。
   - 对照参考：`solver` 的完整闭包 = { utils, dsp, data_mover }（solver→dsp→data_mover 这条传递链把 data_mover 拉了进来，尽管 solver 自己的 `dependsOn` 里没有直接写 data_mover）。

3. **把依赖翻译成 include 路径**。结合 4.1 学到的 `library.json` 约定，写出一个「同时使用 utils 与 dsp」的工程所需的 `-I` 路径：
   - 来自 [utils/library.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/library.json#L1-L17)：`-Iutils/L1/include`
   - 来自 [dsp/library.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/library.json#L1-L17)：`-Idsp/L1/include/hw`
   - 若还涉及 data_mover，再加 `-Idata_mover/L1/include`（依据 [data_mover/library.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/library.json#L1-L17)）。

**预期结果**：一张清晰的依赖拓扑图（主干 solver→dsp→data_mover→utils + security→utils + 四孤岛），以及对 dsp 依赖闭包的准确说明，外加一份能用的 `-I` 路径清单。

**待本地验证**：拓扑与闭包可纯靠阅读得出；`-I` 路径若要验证，可在本地检出仓库后用这些路径实际编译一个引用 dsp + utils 头件的小程序。

---

## 6. 本讲小结

- Vitis_Libraries 是**单仓库**：顶层 9 个库目录 + 一组跨库配置文件（`dependency.json`、`platform_map.json`、`Jenkinsfile`）。
- 每个库都用 `library.json` 声明身份与编译需求：`id`/`name` 是身份，`v++.includepaths` 给内核侧、`host.compiler.includepaths` 给主机侧，路径里的 `LIB_DIR` 是占位符；部分库（如 vision）还用 `host.linker.libraries` 声明链接库（OpenCV）。
- `dependency.json` 用 `dependsOn` 表达库与库的**直接**依赖，主干链是 `solver → dsp → data_mover → utils`，`utils` 是公共根；blas/vision/motor_control/ultrasound 是无依赖的孤岛。使用一个库时要把它的**传递依赖闭包**一起引入。
- `platform_map.json` 把逻辑平台名（`vck190` 等）映射到具体的 `.xpfm` 名，目前只列了 4 个 Versal/AIE 平台，让脚本与平台版本解耦。
- 顶层 `Jenkinsfile` 只有两行，靠 Jenkins 共享流水线库 `pipeline-library` 的 `FullVitisLibPipeline` 驱动所有库的 CI；每个库内部还有自己的薄 `Jenkinsfile`，形成「薄入口 + 厚共享库」的两层结构。
- 各库内部遵循统一的目录约定：`L1/`（原语）、`L2/`（内核/示例）、部分库有 `L3/`（应用流水线），外加共有的 `README.md`、`docs`、`ext`、`library.json`、`Jenkinsfile`、`LICENSE.txt`。

## 7. 下一步学习建议

本讲只讲了「骨架」（目录与配置），还没讲「血肉」（每一层里到底放什么、怎么跑）。建议接下来：

1. **u1-l3（L1/L2/L3 设计哲学与 PL/AIE 范式）**：搞清楚 `L1`/`L2`/`L3` 三层各自的目标、产物，以及 PL 与 AIE 两种开发范式在本讲看到的目录里如何体现（例如 dsp 的 `L1/include/hw` vs `L1/include/aie`）。
2. **u2-l1（搭建 Vitis/XRT 环境）**：本讲的 `platform_map.json` 在那里会真正被用到——你会亲手 `source` Vitis 设置脚本、用逻辑平台名跑工具。
3. 想提前感受 CI 细节的读者，可以跳到 **u14-l1（测试基础设施与 CI）**，那里会讲 `description.json` 如何声明每个 L1 用例的流程与平台白名单。
