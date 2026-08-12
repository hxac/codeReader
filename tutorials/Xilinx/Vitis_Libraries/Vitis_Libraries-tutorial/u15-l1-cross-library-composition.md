# 依赖图与跨库组合

## 1. 本讲目标

Vitis_Libraries 是一个**单体多库**（monorepo）仓库：九个加速库（utils、data_mover、dsp、solver、blas、vision、security、motor_control、ultrasound）并排放在同一棵目录树下，彼此并不孤立。有的库（solver）直接站在另一个库（dsp）的肩膀上，有的库（dsp）又会把第三个库（data_mover）的 PL 内核源码原样编译进自己的工程。

本讲的目标是让你学会「跨库组合」这一工程技能：

1. 读懂 `dependency.json` 表达的依赖图，并能手算任意库的**传递依赖闭包**。
2. 掌握「多库 include 路径」在工程上的两种真实写法：`description.json` 里的 `LIB_DIR/../<lib>/...` 与 Makefile 里的 `$(XFLIB_DIR)/../<lib>/...`。
3. 看懂 `library.json` 为什么**只声明本库内部路径**，跨库路径要另由构建系统补上。
4. 用真实源码验证两条复用链：**solver 复用 dsp**（AIE 头件 + 命名空间 + 测试脚本）、**dsp 复用 data_mover**（连源码一起编译进 container）。

学完后，当你的工程要同时用上多个 Vitis 库时，你能正确写出 `include` 路径列表，并预知遗漏某个传递依赖会导致的编译错误。

## 2. 前置知识

- **L1/L2/L3 与 PL/AIE**：本讲大量引用 L1（原语头件）与 L2（内核 + 示例）的目录，并区分 PL（FPGA，HLS）与 AIE（AI Engine，ADF 图）两条路线。若不熟悉，请先读 u1-l3。
- **library.json 的作用**：每个库用一份 `library.json` 声明身份（`id`/`name`）与编译需求（内核侧 `v++.includepaths`、主机侧 `host.compiler.includepaths`），其中 `LIB_DIR` 是一个占位符，工具链会替换为「本库根目录」。详见 u1-l2。
- **L2 系统构建三段式**：`v++ -c`（编译内核为 XO）→ `v++ -l`（链接）→ `v++ --package`（打包），以及 `description.json` 如何声明 `containers`（PL 内核源）与 `flow`。详见 u5-l1、u6-l3。
- **PL 搬运器 mm2s/s2mm**：把 DDR 的连续布局翻译成 AXI Stream 喂给 AIE 图的协议翻译器。详见 u5-l2。

一句话回顾：**本库的东西用 `LIB_DIR/...`；别人的东西用 `LIB_DIR/../<别的库>/...`**。本讲就是把这句话拆开讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `dependency.json` | 顶层依赖图，声明九个库之间的**直接**依赖关系。 |
| `utils/library.json` | utils 库的身份与 include 路径（最底层、无依赖）。 |
| `data_mover/library.json` | data_mover 库的身份与 include 路径（依赖 utils）。 |
| `dsp/library.json` | dsp 库的身份与 include 路径（依赖 utils、data_mover）。 |
| `solver/library.json` | solver 库的身份与 include 路径（依赖 utils、dsp）。 |
| `solver/L1/include/aie/qrd_traits.hpp` | solver 的 AIE QR 分解 traits，`#include "fir_utils.hpp"` 并 `using namespace ::xf::dsp::aie`，是 solver→dsp 复用的铁证。 |
| `solver/L1/include/aie/qrd_kernel.hpp` | solver 的 AIE QR 内核类，参与构成依赖 dsp 基础设施的 qrd 图。 |
| `solver/L2/tests/aie/qrd/Makefile` | solver AIE 测试的构建文件，用 `DSP_DIR ?= ../dsp` 和 `AIE_CXXFLAGS` 把 dsp 的头件路径接进来。 |
| `solver/L2/benchmarks/gesvj/Makefile` | solver PL/HLS 基准的主机构建文件，用 `-I $(XFLIB_DIR)/../utils/L1/include` 接 utils。 |
| `dsp/L2/tests/aie/matrix_mult_with_datamover/description.json` | dsp 复用 data_mover 的端到端样例：声明 data_mover 的 include 路径，并把 data_mover 的 `mm2s.cpp/bmm2s.cpp/s2mm.cpp` 编译进自己的 container。 |
| `dsp/L2/tests/aie/matrix_mult_with_datamover/Makefile` | 同一样例的 Makefile，用 `VPP_FLAGS += -I .../data_mover/...` 镜像同样的跨库路径。 |

## 4. 核心概念与源码讲解

### 4.1 dependency.json 依赖图

#### 4.1.1 概念说明

九个库并非平级。`dependency.json` 用一张「库 → 直接依赖列表」的表，记录谁站在谁的肩膀上。它的结构极其简单：一个数组，每个元素是 `{ "lib": <库名>, "dependsOn": [<直接依赖库>...] }`。`dependsOn` 只列**直接**依赖，传递依赖要你自己算（见 4.1.3）。

读这张表有两个关键认知：

- **utils 是多数库的公共根**：data_mover、security、dsp、solver 都直接依赖 utils，因为 utils 提供流式原语（axi_to_stream、stream_dup、uram_array 等）几乎所有库都用得到。
- **存在一条主干链**：`utils ← data_mover ← dsp ← solver`。越靠右的库层次越高，复用越多。

而 `blas`、`vision`、`motor_control`、`ultrasound` 的 `dependsOn` 为空——它们是**无依赖孤岛**，工程上可以单独引入而不牵连其他库。

#### 4.1.2 核心流程

`dependency.json` 的消费流程是：

1. 工具链/CI 读取这张表，得到每个库的直接依赖。
2. 对「我要用某个库」的需求，递归展开求**传递依赖闭包**（transitive closure）。
3. 把闭包里**所有库**的根目录都纳入工程（加入 `-I` 路径、加入 container 源等），否则编译会因找不到头件而失败。
4. 闭包即「最小引入集」：少一个就编译不过，多一个只是冗余。

#### 4.1.3 源码精读

先看主干链上的四条直接依赖记录：

[solver/L2/... 之外的顶层 dependency.json，solver 与 dsp 记录 — `dependency.json`:29-35](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L29-L35) 声明 `solver` 直接依赖 `["utils", "dsp"]`。

[dsp 记录 — `dependency.json`:12-18](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L12-L18) 声明 `dsp` 直接依赖 `["utils", "data_mover"]`。

[data_mover 记录 — `dependency.json`:6-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L6-L11) 声明 `data_mover` 直接依赖 `["utils"]`。

[utils 记录 — `dependency.json`:40-43](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dependency.json#L40-L43) 声明 `utils` 无依赖（`dependsOn: []`），是这条链的根。

把这三段连起来读，就得到了 `solver → dsp → data_mover → utils` 的主干。

#### 4.1.4 传递闭包的算法

「直接依赖」不够用——你要用 solver 时，只引入 solver 与 dsp 会因为找不到 data_mover、utils 的头件而编译失败。真正需要的是**传递依赖闭包**。设直接依赖函数为 \(\text{deps}(L)\)，闭包 \(\text{closure}(L)\) 是下面的最小不动点：

\[
\text{closure}(L) \;=\; \text{deps}(L) \;\cup\; \bigcup_{M \,\in\, \text{deps}(L)} \text{closure}(M)
\]

手算 `closure(solver)`：

\[
\begin{aligned}
\text{deps}(\text{solver}) &= \{\text{utils},\ \text{dsp}\} \\
\text{closure}(\text{dsp}) &= \text{deps}(\text{dsp}) \cup \text{closure}(\text{data\_mover}) \cup \text{closure}(\text{utils}) \\
&= \{\text{utils},\ \text{data\_mover}\} \cup \{\text{utils}\} \cup \{\} \\
&= \{\text{utils},\ \text{data\_mover}\} \\
\text{closure}(\text{utils}) &= \{\} \\
\therefore\ \text{closure}(\text{solver}) &= \{\text{utils},\ \text{dsp}\} \cup \{\text{utils},\ \text{data\_mover}\} \\
&= \{\text{utils},\ \text{data\_mover},\ \text{dsp}\}
\end{aligned}
\]

结论：**要用 solver，必须同时把 solver、dsp、data_mover、utils 四个库都引入工程**。这个闭包就是后续 4.2 要写的 include 路径清单的依据。

伪代码形式：

```
closure(L):
    result = {}
    stack  = deps(L)
    while stack 非空:
        m = stack.pop()
        if m not in result:
            result.add(m)
            stack.extend(deps(m))     # 递归并入 m 的依赖
    return result
```

#### 4.1.5 代码实践

**实践目标**：手算所有九个库的闭包，画出依赖拓扑图。

**操作步骤**：

1. 打开 `dependency.json`，逐条记录直接依赖。
2. 用 4.1.4 的算法，对每个库求闭包。
3. 把结果整理成「库 → 闭包」对照表。

**需要观察的现象**：

- `solver`、`dsp`、`data_mover`、`security` 四个库的闭包里都含 `utils`。
- 只有 `solver` 的闭包里同时出现 `dsp` 与 `data_mover`（它是层次最高的库）。
- `blas`/`vision`/`motor_control`/`ultrasound` 的闭包为空（孤岛）。

**预期结果**：

| 库 | 直接依赖 | 闭包 |
| --- | --- | --- |
| utils | ∅ | ∅ |
| data_mover | {utils} | {utils} |
| security | {utils} | {utils} |
| dsp | {utils, data_mover} | {utils, data_mover} |
| solver | {utils, dsp} | {utils, data_mover, dsp} |
| blas / vision / motor_control / ultrasound | ∅ | ∅ |

**待本地验证**：上表由源码静态推出，可直接对照 `dependency.json` 核对；无须运行任何命令。

#### 4.1.6 小练习与答案

**练习 1**：若未来 `solver` 又新增了对 `blas` 的依赖，`closure(solver)` 会变成什么？

**参考答案**：新增 `blas` 后，`deps(solver) = {utils, dsp, blas}`。因 `blas` 无依赖，闭包只在原结果上并上 `blas`：`closure(solver) = {utils, data_mover, dsp, blas}`。

**练习 2**：为什么 `dsp` 既要直接依赖 `utils`、又通过 `data_mover` 间接依赖 `utils`？这是冗余吗？

**参考答案**：不是冗余，而是「显式优于隐式」。`dsp` 自身的 AIE/PL 头件会直接用到 utils 的流式原语（直接依赖），同时它的 data_mover 调用链也依赖 utils（间接依赖）。显式声明直接依赖，能保证即便将来 `data_mover` 不再依赖 `utils`，`dsp` 仍能独立编译。

---

### 4.2 多库 include 路径

#### 4.2.1 概念说明

跨库复用分两层：**声明层**（`dependency.json` 说「我依赖谁」）与**工程层**（构建系统把「谁」的头件路径接进编译命令）。`dependency.json` 本身**不会**自动变成 `-I` 标志——它是给人和 CI 看的契约；真正的 `-I` 要么写在每个用例的 `description.json` 里（被工具链消费），要么写在 Makefile 里（被 make 消费）。

这里有一个贯穿全库的**寻址约定**：

- `LIB_DIR`（出现在 `library.json`、`description.json`）= **本库根目录**的占位符。
- `XFLIB_DIR`（出现在 Makefile）= 同样指「本库根目录」，由公共 `utils.mk` 设定。
- 想引用**同级兄弟库**，就写 `LIB_DIR/../<兄弟库>/...` 或 `$(XFLIB_DIR)/../<兄弟库>/...`——`..` 退到仓库根，再进兄弟库。

注意：`library.json` 里**只**出现 `LIB_DIR/L1/include` 这样的「本库内部」路径，**从不**出现 `LIB_DIR/../别的库`。跨库路径一律下沉到用例的 `description.json` / Makefile。这是一种刻意的分层：库的「自描述」只管自己，跨库组合由使用方负责。

#### 4.2.2 核心流程

把多个库接入一个工程的标准动作：

1. **确定闭包**：用 4.1 的算法算出你要用的库的全部传递依赖。
2. **逐库加路径**：对闭包里每个库，加它的 `library.json` 所声明的 include 路径（内核侧进 `v++.includepaths` / `VPP_FLAGS`，主机侧进 `host.compiler.includepaths` / `CXXFLAGS`，AIE 侧进 `AIE_CXXFLAGS`）。
3. **写法对齐**：`description.json` 用 `LIB_DIR/../<lib>/...`，Makefile 用 `$(XFLIB_DIR)/../<lib>/...`，两者语义相同、须保持一致。
4. **加 container 源**（仅当要编译别库的 PL 内核）：在 `description.json` 的 `containers` 里列出别库的 `.cpp` 源（见 4.4）。
5. **验证**：少加一个库的路径，编译会在 `#include` 处报「找不到头件」——这是依赖遗漏的最直接信号。

#### 4.2.3 源码精读

**先看 `library.json` 只管自己。** utils、dsp、data_mover、solver 四个库的 include 路径都只指向 `LIB_DIR` 内部：

[utils/library.json 内核侧 include — `utils/library.json`:5-9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/library.json#L5-L9) 只有 `LIB_DIR/L1/include`。

[dsp/library.json 内核侧 include — `dsp/library.json`:5-9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/library.json#L5-L9) 只有 `LIB_DIR/L1/include/hw`。

[data_mover/library.json 内核侧 include — `data_mover/library.json`:5-9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/data_mover/library.json#L5-L9) 只有 `LIB_DIR/L1/include`。

[solver/library.json include（含 L2） — `solver/library.json`:5-9](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/library.json#L5-L9) 是 `LIB_DIR/L1/include` 与 `LIB_DIR/L2/include`，仍在自己内部。

注意 solver 的 `library.json` 比其他库多列了 `L2/include` 和主机侧的 `ext/xcl2`（[solver/library.json:10-19](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/library.json#L10-L19)），但依然没有 `../dsp` 或 `../utils`——跨库路径不在这里。

**跨库路径出现在用例层。** PL/HLS 路线看 solver 基准的主机编译行：

[solver 基准接入 utils — `solver/L2/benchmarks/gesvj/Makefile`:106](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/benchmarks/gesvj/Makefile#L106) 用 `-I $(XFLIB_DIR)/../utils/L1/include` 把兄弟库 utils 接进来（`XFLIB_DIR` 是 solver 根，`../utils` 退到仓库根再进 utils）。

AIE 路线看 dsp 接入 data_mover 的两处镜像写法：

[dsp 用例 description.json 接入 data_mover — `dsp/L2/tests/aie/matrix_mult_with_datamover/description.json`:78-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/matrix_mult_with_datamover/description.json#L78-L87) 在 `v++.compiler.includepaths` 里写 `LIB_DIR/../data_mover/L1/include` 与 `LIB_DIR/../data_mover/L2/src/sw/datamover`。

[dsp 用例 Makefile 镜像同一路径 — `dsp/L2/tests/aie/matrix_mult_with_datamover/Makefile`:163](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/matrix_mult_with_datamover/Makefile#L163) 用 `VPP_FLAGS += -I $(XFLIB_DIR)/../data_mover/L1/include -I $(XFLIB_DIR)/../data_mover/L2/src/sw/datamover ...`。

两条路线、两种文件（`.json` 与 `Makefile`）、同一个约定：**`LIB_DIR/../<lib>` 与 `$(XFLIB_DIR)/../<lib>` 是同一件事的两种写法**。

> ⚠️ 一个易踩的坑：solver 在 `L1/include/hw/utils/` 下有一个**本库自带**的 `x_matrix_utils.hpp`（[solver/L1/include/hw/utils/x_matrix_utils.hpp:22](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/utils/x_matrix_utils.hpp#L22)）。当 solver 的 hw 头件写 `#include "utils/x_matrix_utils.hpp"` 时，它解析到的是**这个本地副本**，而不是 utils 库的 `xf_utils_hw/` 头件。别把「solver 里的 `utils/` 子目录」与「utils 加速库」混为一谈——前者是 solver 自带的矩阵小工具，后者是独立的底层库。两者同名却不同源。

#### 4.2.4 代码实践

**实践目标**：为「同时用到 utils 与 dsp」的工程写出完整的 `-I` 路径列表。

**操作步骤**：

1. 据 `dependency.json` 算闭包：`closure(dsp) = {utils, data_mover}`。所以同时用 dsp 与 utils，等价于「用 dsp」，闭包是 `{utils, data_mover}`。
2. 查每个库的 `library.json`，取它们的内核侧 include 路径：
   - utils → `L1/include`
   - data_mover → `L1/include`
   - dsp → `L1/include/hw`
3. 假设你的工程在仓库内，三个库根分别是 `utils/`、`data_mover/`、`dsp/`，写出 `-I` 列表。

**需要观察的现象**：若只写了 dsp 与 utils、漏掉 data_mover，单独编译 dsp 的某些 hw 头件也许能过；但一旦触发到间接依赖 data_mover 的代码路径，就会报 `data_mover/...: No such file or directory`。

**预期结果**（内核侧，相对仓库根）：

```
-I utils/L1/include \
-I data_mover/L1/include \
-I dsp/L1/include/hw
```

若沿用 Vitis 用例的 `XFLIB_DIR` 风格（`XFLIB_DIR` 设为「本工程所属库的根」，这里以 dsp 为锚）：

```
-I $(XFLIB_DIR)/L1/include/hw \
-I $(XFLIB_DIR)/../utils/L1/include \
-I $(XFLIB_DIR)/../data_mover/L1/include
```

**待本地验证**：以上路径由源码静态推出。在你自己的 Vitis 工程里实际编译时，请以 `v++`/`aiecompiler` 报错信息为准微调顺序与目录名。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `library.json` 故意不写 `LIB_DIR/../<别的库>`？

**参考答案**：为了职责分层。`library.json` 是库的「自描述」，只对自己的头件负责；跨库组合是「使用方」的事（用例知道自己的依赖闭包）。若 `library.json` 写死兄弟库路径，会强迫每个库都假设兄弟库一定存在、且相对位置固定，破坏库的可独立分发性。

**练习 2**：`LIB_DIR` 和 `XFLIB_DIR` 各出现在哪类文件里？它们指向同一个目录吗？

**参考答案**：`LIB_DIR` 出现在 `library.json` 与 `description.json`（工具链消费）；`XFLIB_DIR` 出现在 Makefile（make 消费）。两者都指向「当前库的根目录」，是同一概念在两套构建体系里的不同名字。

---

### 4.3 solver 复用 dsp（AIE 路线）

#### 4.3.1 概念说明

solver 的 AIE 实现并不从零造轮子。它直接复用 dsp 库的 AIE 基础设施——既复用**头件**（如 `fir_utils.hpp`），也复用**命名空间**（`xf::dsp::aie`），甚至复用 dsp 的**整套测试脚本**（`paramenv.py`、`diff_exit.tcl`、`tb_gen.py`）。这是一种比「include 一个头件」深得多的复用：solver 的 AIE 测试目录几乎是「站在 dsp 测试框架之上」的薄封装。

复用的技术前提正是 4.2 的 include 路径机制：solver 的 AIE 测试 Makefile 用一个变量 `DSP_DIR ?= ../dsp` 指向兄弟 dsp 库，再把 dsp 的若干目录加进 `AIE_CXXFLAGS`，于是 solver 源码里 `#include "fir_utils.hpp"` 就能解析到 `dsp/L1/include/aie/fir_utils.hpp`（全仓库只有这一份）。

#### 4.3.2 核心流程

solver→dsp 的复用链：

1. Makefile 定义 `DSP_DIR ?= ../dsp`（指向兄弟 dsp 库）。
2. `AIE_CXXFLAGS` 追加 `-I $(XFLIB_DIR)/$(DSP_DIR)/L1/include/aie` 等 dsp 路径。
3. solver 的 AIE 头件（如 `qrd_traits.hpp`）写 `#include "fir_utils.hpp"`，编译器在 dsp 的路径里找到它。
4. solver 代码 `using namespace ::xf::dsp::aie;` 直接调用 dsp 命名空间里的工具。
5. 测试目标调用 `$(XFLIB_DIR)/$(DSP_DIR)/L2/tests/aie/common/scripts/*.py` 复用 dsp 的测试总线。

#### 4.3.3 源码精读

**铁证一：solver 直接 include dsp 的头件并使用 dsp 命名空间。**

[qrd_traits.hpp 引入 fir_utils 与 dsp 命名空间 — `solver/L1/include/aie/qrd_traits.hpp`:26-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_traits.hpp#L26-L31) 里有两行关键代码：第 27 行 `#include "fir_utils.hpp"`，第 31 行 `using namespace ::xf::dsp::aie;`。`fir_utils.hpp` 全仓库仅存在于 `dsp/L1/include/aie/`，`xf::dsp::aie` 正是 dsp 库的 AIE 命名空间——solver 毫无掩饰地直接使用 dsp 的符号。

**铁证二：Makefile 用 DSP_DIR 把 dsp 路径接进来。**

[定义兄弟库指针 — `solver/L2/tests/aie/qrd/Makefile`:83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/tests/aie/qrd/Makefile#L83) `DSP_DIR ?= ../dsp` 把 dsp 库定位为 solver 的兄弟目录。

[把 dsp 目录加入 AIE 编译标志 — `solver/L2/tests/aie/qrd/Makefile`:169](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L2/tests/aie/qrd/Makefile#L169) 这条 `AIE_CXXFLAGS +=` 同时列出 solver 自己的路径（`$(XFLIB_DIR)/L1/include/aie` 等）与 dsp 的路径（`$(XFLIB_DIR)/$(DSP_DIR)/L1/include/aie`、`$(DSP_DIR)/L2/include/aie`、`$(DSP_DIR)/L2/tests/aie/common/inc`）。`$(XFLIB_DIR)/$(DSP_DIR)` 即「solver 根 / `../dsp`」= dsp 库根。

**铁证三：复用 dsp 的测试脚本。** 在同一份 Makefile 的运行目标里，solver 调用的是 dsp 的脚本，例如 `$(XFLIB_DIR)/$(DSP_DIR)/L2/tests/aie/common/scripts/paramenv.py`、`diff_exit.tcl`、`tb_gen.py`（这些行在第 169 行附近的运行 recipe 中大量出现）。这意味着 solver 的 AIE 测试**不是**自己写一套比对/生成框架，而是直接调用 dsp 已经搭好的同一套「UUT/REF 双图比对 + 误差门限」总线（详见 u6-l2、u14-l3）。

**背景：被复用代码所在的 solver 内核。** [qrd_kernel.hpp 的 GramSchmidt 内核类 — `solver/L1/include/aie/qrd_kernel.hpp`:22-39](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/aie/qrd_kernel.hpp#L22-L39) 是 solver 用修正 Gram-Schmidt 做 QR 分解的 AIE 内核类（`xf::solver` 命名空间）。它本身只 include `<aie_api/aie.hpp>` 与 `<adf.h>`，但与它同目录的 `qrd_traits.hpp`（4.3 铁证一）依赖 dsp——也就是说，整张 qrd AIE 图的编译都离不开 dsp 路径。

#### 4.3.4 代码实践

**实践目标**：在不改源码的前提下，验证 `fir_utils.hpp` 的确来自 dsp 库。

**操作步骤**：

1. 在仓库根执行（只读查找）：`find . -name fir_utils.hpp`。
2. 确认它只出现在 `./dsp/L1/include/aie/fir_utils.hpp`，solver 目录下没有副本。
3. 再查 solver 有多少 AIE 头件 include 了它：在 `solver/L1/include/aie/` 下搜索 `#include "fir_utils.hpp"`。

**需要观察的现象**：

- `find` 只返回 dsp 下的唯一一份。
- solver 下有多个头件（`qrd_traits.hpp`、`qrd_utils.hpp`、`cholesky.hpp` 等）include 它，但 solver 自己**没有**该文件——证明它们编译时必然解析到 dsp 的副本。

**预期结果**：`fir_utils.hpp` 唯一来源是 dsp；solver 通过 Makefile 的 `DSP_DIR` 路径间接复用。若人为从 `AIE_CXXFLAGS` 删掉 `$(DSP_DIR)/L1/include/aie`，这些 solver 头件会立刻报 `fir_utils.hpp: No such file`。

**待本地验证**：上述 `find`/`grep` 可直接在本仓库运行复核。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `DSP_DIR` 从 `../dsp` 改成一个不存在的目录，编译会在哪一步失败、报什么错？

**参考答案**：会在 AIE 图编译（aiecompiler）阶段失败，报 `fatal error: fir_utils.hpp: No such file or directory`，因为 solver 的 `qrd_traits.hpp` 找不到 dsp 的头件。这正说明 solver 的 AIE 代码强依赖 dsp 路径。

**练习 2**：solver 复用 dsp 的「测试脚本」与「头件」相比，哪一种耦合更深？为什么？

**参考答案**：测试脚本耦合更深。include 头件只是「借用符号」，换一份等价头件即可；而复用 `paramenv.py`/`diff_exit.tcl`/`tb_gen.py` 意味着 solver 的测试**流程、参数文件格式、PASS/FAIL 判定逻辑**都与 dsp 绑定，solver 的测试目录是 dsp 测试框架的「特化配置」而非独立实现。

---

### 4.4 dsp 复用 utils / data_mover（含源码级组合）

#### 4.4.1 概念说明

dsp 对下层的复用分两种深度：

- **utils**：utils 是 dsp 的直接依赖（`dependency.json`），dsp 的 PL/HLS 与 AIE 头件在需要流式原语时会用 utils 的 `xf_utils_hw/` 头件。这是一种常规的「include 头件」级复用。
- **data_mover**：dsp 的 `matrix_mult_with_datamover` 等用例把复用做到了**源码级**——不仅 include data_mover 的头件，还把 data_mover 的 PL 内核源文件（`mm2s.cpp`、`bmm2s.cpp`、`s2mm.cpp`）直接写进自己的 `containers`，由 `v++ -c` 编译成 XO、再链进 xclbin。换言之，dsp 这个用例的「数据搬运内核」根本不是 dsp 自己写的，而是从 data_mover 库原样搬来的。

这种源码级组合是 Vitis 跨库复用最彻底的形式：你不需要重写一个 mm2s，直接把 data_mover 的成品 PL 内核编译进系统即可。

#### 4.4.2 核心流程

dsp 复用 data_mover 的端到端流程（以 `matrix_mult_with_datamover` 为例）：

1. `description.json` 的 `v++.compiler.includepaths` 加 `LIB_DIR/../data_mover/L1/include` 与 `.../L2/src/sw/datamover`，让 AIE/PL 代码能 include data_mover 头件。
2. `description.json` 的 `containers` 列出三个 PL 内核源，**路径直接指向 data_mover 库**：`LIB_DIR/../data_mover/L2/src/sw/data_mover/mm2s.cpp` 等。
3. `v++ -c` 把这三个 `.cpp` 各编译成一个 XO（mm2s、bmm2s、s2mm）。
4. `v++ -l` 按 `system.cfg` 把这些 PL 搬运内核与 dsp 的 AIE 图（`libadf.a`）链成系统。
5. 主机程序通过 `xrt::kernel` 按名（`mm2s`/`s2mm`）控制这些搬运内核，喂数据给 AIE 矩阵乘法图、再收回结果。

data_mover 真实存在这些源文件，可对照：`data_mover/L2/src/sw/data_mover/` 下确有 `mm2s.cpp`、`bmm2s.cpp`、`s2mm.cpp`、`data_converter.cpp`。

#### 4.4.3 源码精读

**include 路径：让头件可被解析。**

[dsp 接入 data_mover 头件路径 — `dsp/L2/tests/aie/matrix_mult_with_datamover/description.json`:78-87](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/matrix_mult_with_datamover/description.json#L78-L87) 在 `v++.compiler.includepaths` 写了 `LIB_DIR/../data_mover/L1/include` 与 `LIB_DIR/../data_mover/L2/src/sw/datamover`——4.2 讲的 `LIB_DIR/../<lib>` 约定的活样本。

**源码级组合：把别库的 .cpp 编译进自己系统。**

[container 直接引用 data_mover 的 PL 内核源 — `dsp/L2/tests/aie/matrix_mult_with_datamover/description.json`:133-154](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/matrix_mult_with_datamover/description.json#L133-L154) 这段 `containers` 把三个 `accelerators` 的 `location` 直接写成 `LIB_DIR/../data_mover/L2/src/sw/data_mover/mm2s.cpp`、`.../bmm2s.cpp`、`.../s2mm.cpp`，分别命名为 `mm2s`、`bmm2s`、`s2mm`。这是跨库复用最硬核的证据：dsp 用例的搬运内核源码物理上属于 data_mover 库。

**Makefile 侧的镜像写法。**

[dsp 用例 Makefile 接 data_mover — `dsp/L2/tests/aie/matrix_mult_with_datamover/Makefile`:163](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/matrix_mult_with_datamover/Makefile#L163) `VPP_FLAGS += -I $(XFLIB_DIR)/../data_mover/L1/include -I $(XFLIB_DIR)/../data_mover/L2/src/sw/datamover ...`，与 `description.json` 的路径一一对应。

**编译期开关：选用 PL 搬运器。** [同 description.json 主机侧 symbols — `dsp/L2/tests/aie/matrix_mult_with_datamover/description.json`:64-69](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/matrix_mult_with_datamover/description.json#L64-L69) 定义了 `USING_PL_MOVER=1`，用条件编译在「PL data_mover 搬运」与「AIE/GMIO 搬运」之间切换——可见 data_mover 是一个**可选但被显式启用**的下层。

> 关于 **utils**：`dependency.json` 把 utils 列为 dsp 的直接依赖，dsp 复用 utils 的流式原语属于常规头件级复用（与 4.3 solver 复用 dsp 头件同类）。其工程写法同样是 `$(XFLIB_DIR)/../utils/L1/include`，此处不重复展开。

#### 4.4.4 代码实践

**实践目标**：核对 data_mover 真的提供了 dsp 所编译的那三个 PL 内核源。

**操作步骤**：

1. 在仓库根列出 data_mover 的 PL 内核源目录：查看 `data_mover/L2/src/sw/data_mover/` 下有哪些 `.cpp`。
2. 对照 4.4.3 的 `containers`，确认 `mm2s.cpp`、`bmm2s.cpp`、`s2mm.cpp` 都真实存在。
3. （只读）打开其中 `mm2s.cpp` 的前若干行，确认它是一个带 `m_axi` + AXI Stream 接口的 PL 搬运内核（与 u5-l2 讲的 mm2s 语义一致）。

**需要观察的现象**：目录下文件名与 `containers` 里写的完全一致；这些 `.cpp` 不是 dsp 目录下的文件，而是 data_mover 目录下的文件——证明「编译进 dsp 系统的内核」物理上属于 data_mover。

**预期结果**：

```
data_mover/L2/src/sw/data_mover/
├── bmm2s.cpp
├── data_converter.cpp
├── mm2s.cpp
├── s2mm.cpp
└── spec.json
```

与 `description.json` 第 137–150 行声明的三个 `location` 逐一对应。

**待本地验证**：上述目录列表可直接在本仓库核对；如要实际触发 `v++ -c` 编译这些内核，需要完整 Vitis + Versal 工具链与目标平台（见 u2-l1、u15-l2）。

#### 4.4.5 小练习与答案

**练习 1**：dsp 用 `containers` 编译 data_mover 的 `mm2s.cpp`，与 dsp 自己在 `dsp/L1/tests/hw/mm2s/mm2s.cpp` 另有一个 mm2s，二者是什么关系？

**参考答案**：二者是**两套独立的 mm2s 实现**。data_mover 库的 `mm2s.cpp` 是通用的、参数化的搬运器（`matrix_mult_with_datamover` 选用它）；dsp 自己的 `dsp/L1/tests/hw/mm2s/` 是 dsp 早期为 PL FFT 等场景写的专用搬运测试内核。选哪一套取决于用例：需要通用 4D/描述符式搬运时用 data_mover 版，需要与 dsp 某内核紧耦合的简单突发搬运时可用 dsp 自带版。

**练习 2**：若要让 `matrix_mult_with_datamover` 改用 AIE 原生 GMIO 搬运而不用 data_mover，要改哪里？

**参考答案**：把主机侧 symbol 从 `USING_PL_MOVER=1` 改成 `0`（或启用 `USING_GMIO` 类开关），并从 `containers` 移除三个 data_mover 的 `.cpp`、改在 AIE 图侧用 `GMIO` 节点直连 DDR。注意 GMIO 只能线性搬运、做不到 data_mover 那种运行时可变形状的 tiling（详见 u5-l2、u13-l1），所以改用 GMIO 可能需要主机侧自己重排数据。

---

## 5. 综合实践

**任务**：为一个「用 solver 做 QR 分解」的工程，手算依赖闭包并写出完整的跨库 include 路径清单，再标注每条路径来自哪个库的哪份 `library.json`。

**要求**：

1. 由 `dependency.json` 推出 `closure(solver)`（答案见 4.1.4：`{utils, data_mover, dsp}`）。
2. 列出工程要引入的**四个库根目录**：`utils/`、`data_mover/`、`dsp/`、`solver/`。
3. 分别写出 AIE 路线（`AIE_CXXFLAGS`）与 PL/HLS 路线（`VPP_FLAGS`/`CXXFLAGS`）两套 `-I` 路径，参照本讲真实写法：
   - solver 自己：`$(XFLIB_DIR)/L1/include/aie`、`$(XFLIB_DIR)/L1/include`、`$(XFLIB_DIR)/L2/include`
   - dsp：`$(XFLIB_DIR)/$(DSP_DIR)/L1/include/aie`（`DSP_DIR=../dsp`）
   - data_mover：`$(XFLIB_DIR)/../data_mover/L1/include`
   - utils：`$(XFLIB_DIR)/../utils/L1/include`
4. 若该工程要把 data_mover 的 `mm2s/s2mm` 作为搬运内核，写出对应 `description.json` 的 `containers` 片段（仿 4.4.3）。
5. 自检：从你的清单里**删掉 dsp 路径**，预测编译会在哪个头件报错（应能在 `qrd_traits.hpp` 的 `fir_utils.hpp` 处复现）。

**预期产出**：一份「闭包表 + 两条 `-I` 路径列表 + containers 片段 + 删依赖预测」的工程清单。它能直接作为你真实 Vitis 工程的 `Makefile`/`description.json` 模板起点。

**待本地验证**：第 5 步的「删依赖报错」须在本地 Vitis 环境实际编译验证；前三步可纯静态完成。

## 6. 本讲小结

- `dependency.json` 用 `lib → dependsOn` 记录**直接**依赖；真正要用一个库，须手算其**传递依赖闭包**（`closure(solver) = {utils, data_mover, dsp}`），闭包即「最小引入集」。
- `library.json` **只**声明本库内部路径（`LIB_DIR/...`），从不写兄弟库；跨库路径下沉到用例的 `description.json`（`LIB_DIR/../<lib>/...`）与 Makefile（`$(XFLIB_DIR)/../<lib>/...`），两者是同一约定的两种写法。
- **solver 复用 dsp**（AIE 路线）：`qrd_traits.hpp` 直接 `#include "fir_utils.hpp"` 并 `using namespace ::xf::dsp::aie`，Makefile 用 `DSP_DIR ?= ../dsp` 把 dsp 头件与测试脚本路径接进来——这是头件 + 命名空间 + 测试框架的三重复用。
- **dsp 复用 data_mover**：`matrix_mult_with_datamover` 不仅 include data_mover 头件，还把 data_mover 的 `mm2s.cpp/bmm2s.cpp/s2mm.cpp` 直接写进 `containers` 编译进系统——这是最彻底的源码级组合。
- 易踩的坑：solver 自带 `L1/include/hw/utils/x_matrix_utils.hpp`，是本地副本而非 utils 库；`utils/` 子目录与 utils 加速库同名却不同源，切勿混淆。
- 排错直觉：跨库编译报「找不到头件」，几乎总是闭包里漏了某个库的 `-I` 路径——回到 `dependency.json` 重算闭包即可定位。

## 7. 下一步学习建议

- **u15-l2 完整部署**：把本讲的「闭包 + include 路径」放进真实的 `hw` 构建、SD 卡打包与嵌入式 sysroot 语境，看跨库组合在交付阶段的完整面貌。
- **u14-1 测试基础设施与 CI**：本讲反复出现的 `description.json` 的 `containers`/`flow`/`includepaths` 字段，其机器消费方式与 CI 调度细节在 u14-1 系统讲解。
- **继续阅读源码**：精读 `dsp/L2/tests/aie/matrix_mult_with_datamover/system.cfg`（看三个 data_mover 内核的 `nk`/`sp`/`sc` 如何与 AIE 图焊接），以及 `solver/L2/tests/aie/qrd/` 下任一 `description.json`（看 solver 如何复用 dsp 的测试参数与脚本总线），把「跨库组合」从路径层推进到系统拓扑层。
