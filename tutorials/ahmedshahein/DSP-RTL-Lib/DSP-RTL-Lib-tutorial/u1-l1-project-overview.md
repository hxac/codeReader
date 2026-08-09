# 项目总览：DSP-RTL-Lib 是什么

## 1. 本讲目标

读完本讲，你应该能够：

- 用一句话说清楚 **DSP-RTL-Lib（DRL）** 这个项目是做什么的、解决什么问题。
- 理解它的 **mix-and-match（M&M，混搭组合）** 设计理念：为什么作者希望把 DSP 模块做成「可拼装积木」。
- 掌握贯穿全库的 **RTL + 测试台 + GRM 三位一体架构**，明白这三者各自的角色与协作关系。
- 看懂 README 里的 **模块清单与状态表**，知道哪些模块已经成熟可用、哪些还在规划中。

本讲是整本学习手册的起点，不要求你懂任何 Verilog 或 DSP 细节；我们只建立全局认识，细节留给后续讲义。

## 2. 前置知识

本讲是「零起点」的，但有几个名词先建立直觉会更好：

- **DSP（Digital Signal Processing，数字信号处理）**：用数字运算对信号做滤波、变频、生成波形等处理，比如把音频里的噪声滤掉、在通信里把信号搬到另一个频率。
- **RTL（Register Transfer Logic，寄存器传输级）**：一种描述数字硬件的代码写法，Verilog 是最常用的语言之一。RTL 最终可以被综合（翻译）成芯片里的真实电路，用于 ASIC（专用芯片）或 FPGA（可编程逻辑器件）。
- **模块（module）**：在硬件设计里，一段实现某个功能（比如一个滤波器）的、可复用的 RTL 代码单元，类似软件里的「函数/类」。
- **参数化（parameterizable）**：指同一个模块通过调整参数（如位宽、抽头数）就能适配不同需求，不必为每种规格重写代码。
- **可综合（synthesizable）**：RTL 代码能被工具正确转换成实际电路，而不是只能用来仿真。
- **比特真（bit-true）**：一个参考模型和 RTL 在每一个比特的输出上都完全一致，是硬件验证里非常严格的标准。

如果你对其中某些词还陌生，不必担心——本讲会在用到时再解释一遍。

## 3. 本讲源码地图

本讲只依赖两个顶层文件，它们是认识整个项目的「入口」：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md) | 项目的「自我介绍」：定位、设计理念、编码风格、模块清单、运行方式、目录结构都在这里。 |
| [LICENSE](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/LICENSE) | 开源许可证（BSD 2-Clause），告诉你这个库可以怎样被使用和再分发。 |

> 提示：本讲是总览，所以我们主要读 README 和 LICENSE 这两个「说明文档」。真正的 RTL/Octave 源码（位于 `.drl_src_code/` 和 `.drl_param/` 目录）会在后续讲义里逐个精读。本讲会用它们来举例，帮助你看懂架构。

## 4. 核心概念与源码讲解

### 4.1 项目定位与 M&M 理念

#### 4.1.1 概念说明

DRL 的全称是 **DSP RTL Library**，作者是 Ahmed Shahein。一句话定位：

> **一个提供「可参数化、可综合」的通用 DSP 硬件模块的开源库，目标受众是 ASIC/FPGA 前端设计者与 DSP 学习者。**

为什么需要这样的库？因为硬件 DSP 设计有个痛点：很多模块（滤波器、振荡器、变频器等）在各个项目里反复出现，但每次工程师都得从零手写一遍 RTL，既费时又容易出错。DRL 想把这些常用模块做成标准化的「积木」，让设计者拿来即用。

作者提出了一个核心理念叫 **mix-and-match（M&M，混搭组合）**：把一个完整系统拆成基本组件，设计者像搭乐高一样挑选、组合它们，就能拼出一个能工作的系统。README 里举了一个很直观的例子——数字前端（DFE）通常需要混频器、抽取级、信道选择模块，DRL 的目标就是「hopefully one day」把这些基本组件都备齐，让你拼装成系统。

#### 4.1.2 核心流程

DRL 的「M&M 拼装」思想可以用下面的流程来理解：

```
一个完整 DSP 系统（例如数字前端 DFE）
        │  拆解为
        ▼
  ┌──────────────────────────┐
  │ 混频器 │ 抽取级 │ 信道选择 │ ...   ← 各个基本组件
  └──────────────────────────┘
        │  从 DRL 中各取所需、参数化配置
        ▼
   拼装、连线 → 一个能工作的系统
```

从「使用者的视角」看，工作流大致是：

1. 在 DRL 的模块清单里找到需要的组件（如 FIR 滤波器）。
2. 按自己的需求填一份参数配置（参数模板放在 `.drl_param/`）。
3. 用构建脚本 `dsp_rtl_lib.sh` 生成对应的 RTL 并跑回归验证。
4. 把生成好的模块例化（实例化）进自己的系统里，与其他模块连线。

> 注：README 也提到，作者计划在库进入 alpha（稳定）阶段后，提供「以参数直接生成 RTL」的脚本能力，让上述流程更自动化。

#### 4.1.3 源码精读

下面这段 README 是理解 DRL 定位与 M&M 理念的关键，作者在这里亲自阐述了项目目标：

- [README.md:7-7](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L7-L7) —— 说明作者专注 DSP 硬件（前端/RTL）设计，并提出 **mix-and-match（M&M）** 方法：以 DFE 为例，DRL 提供混频器、抽取级、信道选择等基本组件，让设计者「挑选、混搭」成系统。

作者对项目愿景（参数化、可综合、提供 GRM、未来用脚本生成 RTL）的完整阐述在这一段：

- [README.md:9-9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L9-L9) —— 说明 DRL 的目标是提供**最常用的 DSP 组件**，以**可参数化、可综合**的 RTL 形式服务于 ASIC 和/或 FPGA 开发；同时计划提供 **GRM（黄金参考模型，主要是 Octave）**，用于比特真验证（bit-true verification）；并计划在 alpha 阶段后用脚本根据设计参数生成 RTL。

作者还很坦诚地说明了当前实现的定位，这对学习者很重要：

- [README.md:13-13](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L13-L13) —— 当前 RTL 是**常规实现，既不是面积优化也不是功耗优化**；面积/功耗/延迟优化通常与应用或项目相关，所以暂时没有发布。理解这一点能帮你建立合理预期：DRL 重在「正确、清晰、可复用」，而不是极致的面积/功耗。

关于许可证，它决定了你能否自由使用这个库：

- [LICENSE:1-4](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/LICENSE#L1-L4) —— DRL 采用 **BSD 2-Clause 许可证**（Copyright (c) 2019, Ahmed Shahein）。这是一种宽松的开源协议：允许你自由使用、修改和再分发（包括商业用途），只需保留版权声明与免责声明即可。

#### 4.1.4 代码实践

**实践目标**：用自己的话，把「DRL 是什么、为什么要做它」讲清楚。

**操作步骤**：

1. 打开 [README.md](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md)，通读 *Overview* 一节（第 4–13 行）。
2. 思考一个问题：如果不用 DRL，工程师要怎样得到一个 FIR 滤波器的 RTL？他会经历哪些重复劳动？
3. 写一段大约 100 字的中文说明，回答两点：
   - 相比每次手写 DSP RTL，DRL 的优势是什么（提示：参数化、可复用、有验证模型）。
   - M&M 理念让你怎样构建一个系统。

**需要观察的现象**：你会注意到作者并非追求「最快的电路」，而是追求「正确、可参数化、可拼装」。这种价值观会贯穿整本手册。

**预期结果**：你能写出类似「DRL 把常用 DSP 模块做成可参数化、可综合的积木，设计者通过 M&M 混搭即可快速搭建系统，省去重复手写与调试」的总结。

**待本地验证**：本实践无需运行任何命令，是纯阅读与写作任务。

#### 4.1.5 小练习与答案

**练习 1**：README 说当前 RTL「既不是面积优化也不是功耗优化」，那么 DRL 的核心价值到底在哪里？
> **参考答案**：核心价值在于「正确性、可参数化、可复用、可验证」。它提供的是一套清晰可靠的通用模块，让你不必从零造轮子；面积/功耗优化通常与应用强相关，留到具体项目里再做更合适。

**练习 2**：用一句话解释什么是 M&M（mix-and-match）理念，并举一个 README 提到的例子。
> **参考答案**：M&M 是指把系统拆成基本 DSP 组件，设计者按需挑选、组合成完整系统；README 给的例子是数字前端（DFE）由混频器、抽取级、信道选择模块拼装而成。

---

### 4.2 RTL/GRM/测试台三位一体架构

#### 4.2.1 概念说明

DRL 最有特色、也最重要的工程哲学是它的 **三位一体架构**：每一个模块都同时拥有三套相互配合的产物：

1. **RTL（可综合设计代码）**：用 **Verilog 2001** 写成的硬件实现，这是最终要变成芯片电路的部分。
2. **测试台（testbench）**：用 **SystemVerilog 2012** 写的仿真验证平台，负责给 RTL 喂激励、采集输出、做比对。
3. **GRM（Golden Reference Model，黄金参考模型）**：用 **Octave**（兼容 MATLAB 语法）写的高层参考模型，被认为是「标准答案」。

为什么要有三套？因为硬件验证的关键难点是：**你怎么知道你写的 RTL 是对的？** 单凭 RTL 自身没法回答这个问题——你需要一个「可信的正确答案」来对照。GRM 就是这个正确答案：它是用成熟的数学软件实现的高层算法，逻辑直观、容易确认正确。然后让测试台把同一批输入同时喂给 GRM 和 RTL，逐个样本比对它们的输出，如果每个比特都一致（**比特真，bit-true**），就说明 RTL 实现正确。

这就是「三位一体」的协作关系：**GRM 提供标准答案 → 测试台负责喂激励与比对 → RTL 接受检验**。三者缺一不可。

> 名词解释：
> - **激励（stimuli）**：送给被测模块的输入信号序列。
> - **响应（response）**：被测模块处理激励后产生的输出序列。
> - **回归验证（regression verification）**：用一组预设的测试用例反复验证，确保改动后结果仍正确。

#### 4.2.2 核心流程

比特真验证的闭环可以用下面的流程表示：

```
            ┌───────────────┐
            │   Octave GRM  │  ← 高层算法，"标准答案"
            └───────────────┘
        ① 生成激励 .dat          ② 生成期望响应 .dat
            │                          │
            ▼                          │
   ┌────────────────┐                  │
   │ SystemVerilog  │  ③ 读激励         │
   │   测试台 (TB)   │──────────►  ④ RTL 处理 → 实际输出
   └────────────────┘                  │
            │  ⑤ 在采样边沿逐样本比对     │
            ▼                          ▼
       实际输出  vs  期望响应  ──►  相等？ → error_count → PASSED/FAILED
```

关键步骤解读：

1. GRM 先在 Octave 里计算，把**输入激励**和**期望响应**都导出成数据文件（如 `.dat`）。
2. 测试台读取激励文件，按时序逐个样本喂给 RTL。
3. RTL 产出实际输出。
4. 测试台在合适的时钟边沿，把 RTL 实际输出与 GRM 的期望响应逐样本（逐比特）比对。
5. 累计不匹配次数（`error_count`），据此判定 PASSED 或 FAILED。

#### 4.2.3 源码精读

README 里有一句直接点明三位一体架构的话，是本节的「总纲」：

- [README.md:11-11](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L11-L11) —— 明确 DRL 将提供：**用 Verilog 2001 开发的可综合 RTL**、**用 SystemVerilog 2012 开发的测试台**、以及 **Octave 黄金参考模型（GRM）**。

而 GRM 的用途（比特真验证）则在这里：

- [README.md:9-9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L9-L9) —— 说明 GRM 的目的是用于 **bit-true verification（比特真验证）** 和构建「无损」系统（building time-lossly systems，意指不引入量化误差的参考实现）。

这套架构在仓库里是有实体体现的——每一个模块目录下都同时包含这三类产物。以 CIC 抽取滤波器 `filt_cicd` 为例（这些文件确实存在于 `.drl_src_code/filt_cicd/` 下，供你建立直觉，详细讲解在后续讲义）：

| 三位一体角色 | 对应文件示例（filt_cicd 模块） |
|------------|------------------------------|
| RTL（Verilog 2001 设计） | `.drl_src_code/filt_cicd/rtl/filt_cicd.v` |
| 测试台（SystemVerilog 2012） | `.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv` |
| GRM（Octave 参考模型） | `.drl_src_code/filt_cicd/octave/CICFilter.m` |

可以看到三者共存于同一个模块目录里，这正是「三位一体」的物理证据。

#### 4.2.4 代码实践

**实践目标**：亲手在仓库里找到「三位一体」的三类文件，建立直觉。

**操作步骤**：

1. 在仓库根目录列出模板源码目录：`ls .drl_src_code/`，你会看到 8 个模块目录。
2. 任选一个模块（例如 `filt_cicd`），列出它的内容：`ls -R .drl_src_code/filt_cicd/`。
3. 找出分别对应 RTL、测试台、GRM 的文件（提示：分别在 `rtl/`、`sim/testbench/`、`octave/` 子目录下）。

**需要观察的现象**：每个模块都同时拥有 `rtl/`、`sim/testbench/`、`octave/` 三类子目录，验证了三位一体架构是全库统一约定。

**预期结果**：你能在 `filt_cicd` 模块里至少找到 1 个 `.v`（RTL）、1 个 `.sv`（测试台）、1 个 `.m`（Octave GRM）文件，并能说出它们各自的角色。

**待本地验证**：上述 `ls` 命令的输出取决于本地仓库实际文件；本讲已确认这些文件存在，但你亲手运行能加深印象。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要 GRM？只用 RTL 和测试台不行吗？
> **参考答案**：测试台只是「喂激励、采输出」的工具，它本身不知道「正确答案」是什么。GRM 用成熟的数学软件实现高层算法，提供可信的「标准答案」，让测试台有可比对的期望响应，否则无法判断 RTL 输出是否正确。

**练习 2**：三位一体分别用什么语言/工具？为什么 RTL 用 Verilog 2001 而不是更新的版本？
> **参考答案**：RTL 用 Verilog 2001，测试台用 SystemVerilog 2012，GRM 用 Octave。Verilog 2001 是可综合设计的经典子集，兼容性极好、几乎所有综合工具都支持；而测试台需要更强的语言能力（文件 IO、面向对象等），所以用更现代的 SystemVerilog 2012。

---

### 4.3 模块清单与状态表

#### 4.3.1 概念说明

README 提供了一张 **模块清单与状态表**，告诉你这个库里「现在有什么、将来会有什么」。这对学习者规划路线、对使用者评估可用性都很关键。

表里有两个状态：

- **Stable（稳定）**：模块已经开发完成、经过验证，可以拿来使用与学习。
- **Planned（规划中）**：模块还在计划阶段，仓库里尚无对应的源码。

> 名词速查（README 的 *List of Abbreviations* 节里有完整对照表，见 [README.md:15-31](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L15-L31)）：
> - **FIR / IIR**：有限/无限脉冲响应滤波器。
> - **CIC**：级联积分梳状滤波器（Cascade Integrator Comb），常用于多速率抽取/插值。
> - **MAC**：乘加累加器（Multiply ACCumulator），这里指资源复用型 FIR。
> - **PPD / PPI**：多相抽取 / 多相插值（PolyPhase Decimation / Interpolation）。
> - **NCO**：数控振荡器（Numerically Controlled Oscillator），用来生成正余弦波。
> - **CORDIC**：坐标旋转数字计算机（COordinate Rotation DIgital Computer），用移位相加算三角/旋转。
> - **DC**：直流（Direct Current），通常指直流偏置消除/校准类模块。

#### 4.3.2 核心流程

把 README 的状态表与仓库里真实存在的模块目录对应起来，可以画一张「计划 vs 现实」对照图：

```
README 模块清单                    仓库 .drl_src_code/ 里的实体
─────────────────────────────     ─────────────────────────────
FIR      (Stable)        ◄────►   filt_fir/
MAC      (Stable)        ◄────►   filt_mac/
CIC      (Stable)        ◄────►   filt_cicd/ (抽取) + filt_cici/ (插值)
PPD      (Stable)        ◄────►   filt_ppd/
PPI      (Stable)        ◄────►   filt_ppi/
NCO      (Stable)        ◄────►   sgen_nco/
CORDIC   (Stable)        ◄────►   sgen_cordic/
DC       (Planned)       ◄────►   (暂无源码)
IIR      (Planned)       ◄────►   (暂无源码)
```

一个值得注意的细节：README 表里 **「CIC」一行对应仓库里的两个模块**——`filt_cicd`（抽取，decimation）和 `filt_cici`（插值，interpolation）。也就是说，CIC 这个家族同时包含抽取器和插值器两种实现。本讲义的模块清单学习路线也据此把 CIC 拆成两篇讲义。

统计一下：**当前共 7 个 Stable 模块族（对应仓库 8 个模块目录），2 个 Planned（DC、IIR）**。这也解释了为什么本学习手册的后续单元会聚焦在 FIR、CIC、多相、信号生成器这几个已成熟模块族上。

#### 4.3.3 源码精读

模块清单与状态表原文如下，这是本节的核心依据：

- [README.md:33-45](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L33-L45) —— 这是 DRL 的 **List of Modules** 表。逐行列出：FIR、MAC、CIC、PPD、PPI 为 **Stable**；DC、IIR 为 **Planned**；NCO、CORDIC 为 **Stable**。注意每行的 Description 列在源文件里目前是空的，模块含义需结合上方的缩写表理解。

缩写对照表（帮助你读懂模块名）：

- [README.md:15-31](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L15-L31) —— **List of Abbreviations** 表，给出了 FIR、IIR、CIC、MAC、TF、DF、PPD、PPI、NCO、CORDIC、LFSR、RTL、DSP、DRL 等缩写的完整英文释义。遇到不认识的模块名，先来这里查。

#### 4.3.4 代码实践

**实践目标**：把 README 的「计划」与仓库的「现实」对齐，亲手列出 Stable 与 Planned 模块。

**操作步骤**：

1. 阅读 [README.md:33-45](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L33-L45) 的模块表。
2. 运行 `ls .drl_src_code/` 查看仓库里实际存在的模块目录。
3. 做两张清单：
   - **Stable 模块**：README 标 Stable 的，哪些在仓库里有源码目录？（应为 FIR、MAC、CIC(=cicd+cici)、PPD、PPI、NCO、CORDIC）
   - **Planned 模块**：README 标 Planned 的，仓库里有没有对应目录？（应为 DC、IIR，目前无目录）
4. 借助 [README.md:15-31](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L15-L31) 的缩写表，用一句中文写出每个 Stable 模块的用途。

**需要观察的现象**：README 的 Stable 条目都能在仓库里找到对应源码；Planned 条目（DC、IIR）则找不到目录。CIC 一行对应两个目录（抽取 + 插值）。

**预期结果**：你能产出两张清晰的清单，并发现「CIC = 抽取 + 插值」这一细节。

**待本地验证**：仓库目录列表以你本地 `ls` 结果为准；本讲已确认上述对应关系。

#### 4.3.5 小练习与答案

**练习 1**：README 表里 CIC 只有一行，但仓库里有几个 CIC 相关目录？分别是什么？
> **参考答案**：仓库里有 **两个** CIC 相关目录：`filt_cicd`（CIC 抽取，decimation）和 `filt_cici`（CIC 插值，interpolation）。README 把它们合并成「CIC」一行，状态为 Stable。

**练习 2**：哪些模块是 Planned（规划中）？这意味着什么？
> **参考答案**：**DC** 和 **IIR** 是 Planned。这意味着它们已被列入计划，但目前仓库里还没有对应的源码实现，暂不能使用或学习其实现。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「项目速写」小任务：

**任务**：假设你是一位刚加入团队的 ASIC 前端工程师，需要向同事用 5 分钟介绍 DRL 这个库。请基于本讲内容，准备一份**一页纸的项目速写**（中文，约 300 字），必须包含：

1. **一句话定位**：DRL 是什么（参考 4.1）。
2. **设计理念**：解释 M&M 混搭思想，并举 DFE 的例子（参考 4.1）。
3. **架构亮点**：说明 RTL + 测试台 + GRM 三位一体如何实现比特真验证（参考 4.2，画出 GRM→TB→RTL 的比对关系）。
4. **可用模块**：列出当前 Stable 的模块族，并指出哪些还在 Planned（参考 4.3）。
5. **使用预期**：提醒同事当前 RTL 不是面积/功耗优化的，核心价值在正确性与可复用（参考 [README.md:13-13](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L13-L13)）。

**检查标准**：同事听完后，应该能回答「DRL 解决什么问题」「为什么有三套代码」「现在能用哪些模块」这三个问题。如果他能复述出「GRM 提供标准答案、测试台负责比对、RTL 接受检验」这一闭环，就说明你讲透了三位一体。

> 这个任务无需运行任何命令，是阅读理解与表达练习；但它要求你把本讲的三个最小模块融会贯通。

## 6. 本讲小结

- **DRL（DSP-RTL-Lib）** 是一个提供「可参数化、可综合」通用 DSP 硬件模块的开源库，面向 ASIC/FPGA 前端设计与 DSP 学习，采用 BSD 2-Clause 许可证。
- 核心理念是 **mix-and-match（M&M）**：把系统拆成基本组件，设计者挑选、混搭、参数化后拼装成完整系统（如 DFE = 混频器 + 抽取级 + 信道选择）。
- 全库采用 **三位一体架构**：Verilog 2001 的可综合 RTL + SystemVerilog 2012 的测试台 + Octave 的 GRM，三者协作实现**比特真验证**。
- 验证闭环是：**GRM 产出标准答案 → 测试台喂激励并比对 → RTL 接受检验 → error_count 判定 PASSED/FAILED**。
- 当前 **7 个模块族为 Stable**（FIR、MAC、CIC=cicd+cici、PPD、PPI、NCO、CORDIC），**2 个为 Planned**（DC、IIR）。
- 当前 RTL 是**常规实现，不做面积/功耗优化**，核心价值在正确性、可参数化与可复用。

## 7. 下一步学习建议

本讲建立了全局认识，接下来建议按以下顺序继续：

1. **先摸清仓库骨架**：学习下一讲 [u1-l2 仓库结构与目录组织](u1-l2-repository-structure.md)，搞懂顶层目录（`.drl_src_code/`、`.drl_param/`）与每个模块内部 `rtl/octave/sim/...` 的标准布局——这是后续所有讲义导航的基础。
2. **学会运行 demo**：接着学 [u1-l3 工具链与构建运行流程](u1-l3-toolchain-and-build-flow.md)，掌握 `dsp_rtl_lib.sh` 的用法，亲手跑通一次 CIC 抽取的回归验证。
3. **建立编码风格直觉**：学 [u1-l4 统一编码风格与接口约定](u1-l4-coding-style-and-interface.md)，理解全库统一的复位/使能/时钟约定与命名规范。
4. **进阶阅读建议**：等你跑通 demo、熟悉目录与风格后，再进入「定点数与共享原语」单元，开始读真正的 RTL 源码。

> 阅读源码建议：在进入 RTL 细节前，先把 README 的 *Coding Style*（[README.md:47-53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L47-L53)）和 *Folder Structure*（[README.md:67-80](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L67-L80)）两节扫一遍，这两节是后续讲义的「地图」。
