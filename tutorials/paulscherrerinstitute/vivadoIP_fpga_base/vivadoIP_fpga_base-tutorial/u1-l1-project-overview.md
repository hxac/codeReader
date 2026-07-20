# 项目总览：fpga_base 是什么、解决什么问题

## 1. 本讲目标

读完本讲，你应当能够：

- 用一句话说清楚 **fpga_base** 这个 Vivado IP 核是做什么的、解决 FPGA 设计里的哪一类问题。
- 指出项目的维护者、作者，以及它采用的 **PSI HDL Library License**（LGPL + 固件例外）意味着什么。
- 看懂 README 里那张「依赖清单」，分清 **TCL / VHDL / VivadoIp** 三类依赖各自的用途，并理解 `psi_common` 为什么是不可缺少的。
- 顺着 `Changelog.md` 把这个 IP 的版本演进（从 1.0.0 到 1.4.0）串起来，知道每个版本带来了什么变化。

本讲是整本学习手册的第一篇，不要求你懂 VHDL 或 AXI 协议，只要跟着读文件即可。

## 2. 前置知识

在开始之前，先用大白话解释几个反复出现的名词：

- **FPGA（现场可编程门阵列）**：一种可以通过编写「硬件描述语言」来重新配置内部电路的芯片。你可以把它想象成一块「可以重新接线的面包板」。
- **HDL（Hardware Description Language，硬件描述语言）**：用来描述 FPGA 内部电路的语言，本项目用的是 **VHDL**。写 HDL 像在画电路图，而不是写普通软件。
- **IP 核（Intellectual Property Core）**：一段可以复用、打包好的硬件电路模块，类似于软件里的「库」或「插件」。`fpga_base` 就是一个 IP 核。
- **Vivado**：Xilinx 公司（现属 AMD）出品的 FPGA 开发工具，负责把 HDL「综合」成真实的电路，并把 IP 核「打包」成可以分发的产物。
- **AXI**：一种芯片内部总线协议，CPU（如 MicroBlaze 软核）通过它读写 FPGA 里的寄存器。`fpga_base` 提供的就是一组可以通过 AXI 访问的寄存器。
- **IP-XACT**：描述 IP 核元数据（端口、参数、文件清单）的 XML 标准，Vivado 用它来管理 IP。本仓库里的 `component.xml` 就是 IP-XACT 文件。
- **PSI**：Paul Scherrer Institute（保罗谢勒研究所，瑞士），本项目的归属机构，它维护了一整套 HDL 库。

理解这些之后，你就能把本讲读得更顺。

## 3. 本讲源码地图

本讲只看「项目门面」级别的文件，不深入代码逻辑：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md) | 项目门面：维护者、作者、许可证、依赖清单、一句话功能描述。其中依赖区块会被脚本解析。 |
| [Changelog.md](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/Changelog.md) | 版本演进记录，从 1.0.0 到 1.4.0。 |
| [License.txt](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/License.txt) | PSI HDL Library License 全文，LGPL 加固件例外条款。 |
| [LGPL2_1.txt](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/LGPL2_1.txt) | LGPL 2.1 协议原文（被 License.txt 引用）。 |
| [doc/fpga_base.pdf](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/doc/fpga_base.pdf) | 数据手册（Datasheet），IP 的详细文档。 |
| [scripts/dependencies.py](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py) | 依赖解析脚本，它读取 README 的依赖区块来自动拉取依赖。 |

## 4. 核心概念与源码讲解

### 4.1 项目定位

#### 4.1.1 概念说明

做 FPGA 设计时，几乎每个项目都会用到一些「基础小功能」：想知道当前烧进 FPGA 的固件是哪个版本、是什么时候编译的、想点几颗 LED 看状态、想读几个拨码开关（DIP-Switch）做配置。这些功能每个项目都重复造轮子很烦，于是 PSI 把它们做成一个标准 IP 核，就是 **`fpga_base`**。

README 里的一句话描述说得很直白：

> This IP-core implements basic FPGA functionality (Version readout, LED and DIP-Switches)

也就是说，`fpga_base` 是一个「**基础功能包**」，它把这些常用的小电路打包成一个 Vivado IP，让别的 FPGA 工程可以直接拖进来用，并通过 AXI 总线暴露出一组寄存器供软件读写。

综合后续讲义你会知道，它的核心能力其实是四类：

1. **版本号读出**——软件能读到这个 IP 的版本。
2. **固件编译日期/时间**——记录 FPGA 工程编译（综合）那一刻的年月日时分。
3. **软件编译日期/时间**——CPU 软件启动时把自己的编译时间写回寄存器。
4. **LED 与 DIP 开关**——点亮物理 LED、读取物理拨码开关。

一句话定位：**`fpga_base` 是 PSI HDL 库里的「FPGA 基础设施 IP」，给任何 FPGA 工程提供版本追溯和基础 IO 能力。**

#### 4.1.2 核心流程

从一个使用者视角看，`fpga_base` 在整个系统里的位置可以简化为：

```text
+-------------------+        AXI 总线        +-----------------------+
|   CPU / 软核       |  ===================>  |    fpga_base IP 核      |
|  (MicroBlaze 等)   |   读写寄存器            |  - 版本寄存器          |
+-------------------+                        |  - 固件日期寄存器       |
                                             |  - 软件日期寄存器       |
                                             |  - LED / DIP 寄存器    |
                                             +-----------+-----------+
                                                         |
                                            物理引脚: o_led / i_sw
```

- 软件侧通过 AXI 协议访问寄存器，读到版本、日期等信息。
- 硬件侧把寄存器值连到真实的 LED 引脚、从真实的 DIP 开关引脚采样。
- IP 核本身则由 Vivado 从本仓库的 VHDL 源码「综合 + 打包」生成。

#### 4.1.3 源码精读

README 顶部的元信息和功能描述写明了「谁在做、做什么」：

- 维护者与作者信息，见 [README.md:3-8](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L3-L8)——这里列出了 Maintainer（Waldemar Koprek）和两位 Authors（Oliver Bründler、Goran Marinkovic），都是 PSI 的人员。

- 一句话功能定义，见 [README.md:43-44](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L43-L44)——`# Description` 区块明确说本 IP 实现「Version readout, LED and DIP-Switches」等基础 FPGA 功能。

- 文档指针，见 [README.md:13-14](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L13-L14)——README 指向数据手册 `doc/fpga_base.pdf`，那里有更详细的寄存器与端口说明。

这些就是判断「这个项目是干什么的」的全部一手依据，不依赖任何推测。

#### 4.1.4 代码实践

**实践目标**：把 `fpga_base` 的「定位」从抽象变成可验证的事实。

**操作步骤**：

1. 用阅读器打开本仓库根目录的 `README.md`，定位到 `# Description` 标题。
2. 再打开 `doc/fpga_base.pdf`（如果本地装了 PDF 阅读器），翻到「Introduction / Features」之类的章节。
3. 把 README 的一句话描述和 PDF 里的功能列表对照看。

**需要观察的现象**：README 里提到的功能（Version readout、LED、DIP-Switch），在 PDF 数据手册里应该都能找到对应的寄存器或端口描述。

**预期结果**：你会发现 README 是「精简门面」，PDF 是「完整说明书」，两者描述一致。

**待本地验证**：PDF 的具体章节标题可能随版本略有不同，请以你本地打开的 `doc/fpga_base.pdf` 实际目录为准。

#### 4.1.5 小练习与答案

**练习 1**：`fpga_base` 解决的是「大而全的核心算法」问题，还是「每个 FPGA 工程都要重复实现的基础小功能」问题？

> **答案**：后者。它是基础设施型 IP，提供版本读出、LED、DIP 开关等几乎所有工程都要用到的通用小功能，目的是避免重复造轮子。

**练习 2**：如果不使用 `fpga_base`，一个 FPGA 工程想实现「软件能读到固件编译时间」，需要自己做哪些事？

> **答案**：需要自己定义一组 AXI 寄存器、自己想办法把编译时刻「刻」进硬件（这正是后续讲义会讲的 FDPE 触发器 + INIT 注入技巧）、再写软件读出函数。`fpga_base` 把这些全打包好了。

---

### 4.2 许可证与依赖

#### 4.2.1 概念说明

一个 IP 核能不能被「拿去用、拿去卖」，取决于两件事：**许可证**和**依赖**。

- **许可证**：`fpga_base` 采用的是 **PSI HDL Library License**。它的本质是 **LGPL 2.1 + 一条「固件例外」条款**。这条例外非常关键：它明确允许你把本库编译/综合成「二进制」（包括 **FPGA 比特流 bitstream**、Flash 镜像）后，按你自己的条款去使用、链接、修改、分发。换句话说，**用 `fpga_base` 烧出来的 FPGA 镜像，不受 LGPL 传染性约束**，可以用于商业闭源产品。这对工业场景（比如 PSI 的加速器控制）很重要。

- **依赖**：`fpga_base` 不是孤立的，它依赖一批 PSI 维护的 TCL / VHDL 工具库。这些依赖分成三类，README 里专门用一个「会被脚本解析」的区块列出。

理解依赖的关键是分清三类：

| 类别 | 含义 | 本项目依赖 | 是否仅在开发时需要 |
| --- | --- | --- | --- |
| **TCL** | Vivado 打包/仿真用的 TCL 工具库 | PsiSim、PsiIpPackage、PsiUtil | 多为「for development only」 |
| **VHDL** | 综合 IP 时真正用到的硬件电路库 | **psi_common** | 否（综合必需） |
| **VivadoIp** | 以 Vivado IP 形式发布的依赖 | vivadoIp_fpga_base（自指） | 否 |

> 注：`psi_common` 之所以是「综合必需」的，是因为 1.3.0 版本起，本 IP 改用 `psi_common` 提供的 AXI 从机，而不是自带的旧版（见 Changelog）。

#### 4.2.2 核心流程

依赖的「声明 → 解析 → 拉取」流程是这样的：

```text
README.md 的 Dependencies 区块（被特殊注释包裹）
        |
        | 被脚本读取
        v
scripts/dependencies.py  -->  PsiFpgaLibDependencies.Parse.FromReadme()
        |
        | 自动检查/拉取各依赖到本地
        v
本地具备全部依赖后，才能用 PsiIpPackage 打包 IP
```

这里最巧妙的一点：**依赖清单只维护在 README 一个地方**，脚本去读它，避免「README 写一套、脚本里又写一套」的不一致。README 里专门有一行注释提醒你**不要改格式**：

```text
<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->
```

#### 4.2.3 源码精读

- 许可证声明，见 [README.md:10-11](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L10-L11)——明确说本库发布在 PSI HDL Library License 下，即 LGPL 加额外例外。

- 固件例外条款全文，见 [License.txt:15-22](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/License.txt#L15-L22)——其中第 2 条明确把「FPGA-bitstreams or flash images」纳入「binary」，允许按自己条款分发。

- 依赖清单（被解析区块），见 [README.md:19-32](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L19-L32)——可以看到 TCL 类三个（PsiSim、PsiIpPackage、PsiUtil，均标注 development only）、VHDL 类一个（psi_common ≥ 2.5.0）、VivadoIp 类一个（自指）。

  > 注意一个小细节：PsiSim、psi_common 托管在 **GitHub（公开）**，而 PsiIpPackage、PsiUtil 托管在 **git.psi.ch（PSI 内部）**。所以社区用户能直接拿到前者，后者通常需要 PSI 内部访问或随仓库分发。

- 依赖解析脚本，见 [scripts/dependencies.py:7-10](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py#L7-L10)——它把 README 路径传给 `Parse.FromReadme()`，再交给 `Actions.ExecMain()` 执行实际拉取。脚本本身只有几行，真正的解析逻辑在外部 `PsiFpgaLibDependencies` 包里。

- 运行前提，见 [README.md:34-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L34-L40)——README 提醒要先安装 `PsiLibDependencies` 这个 Python 包才能运行该脚本。

#### 4.2.4 代码实践

**实践目标**：亲手把「依赖关系图」画出来，并验证 `psi_common` 的不可替代性。

**操作步骤**：

1. 打开 [README.md:21-31](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L21-L31) 的 Dependencies 区块。
2. 按下表把三类依赖整理成一张关系图（可以画在纸上或用任意画图工具）：

```text
                    fpga_base (本 IP)
                  /        |          \
            TCL 类       VHDL 类      VivadoIp 类
           /  |  \          |             |
       PsiSim PsiIpPackage  psi_common   (自指)
              PsiUtil      [综合必需]
           [开发期工具]
```

3. 思考并写一段话：**如果缺少 `psi_common`，本 IP 还能正常打包吗？**

**需要观察的现象**：注意 VHDL 类里只有 `psi_common` 一个，且它**没有**标注「for development only」，而 TCL 类几乎都标注了。

**预期结果**（参考答案）：

> **不能正常打包。** `psi_common` 是综合 IP 时真正需要的硬件电路库——根据 Changelog 1.3.0，本 IP 从 1.3.0 起改用 `psi_common` 提供的 AXI 从机（`psi_common_axi_slave_ipif`）。如果缺少它，Vivado 在综合阶段就会因为找不到这个库而报错，IP 根本无法打包成 `component.xml`。相比之下，TCL 类的 PsiSim/PsiIpPackage/PsiUtil 是「开发期打包/仿真工具」，少了它们你是「无法重新打包」，但不是「电路无法综合」——两者性质不同。

**待本地验证**：如果你本地装了 Vivado，可以尝试在缺 `psi_common` 时运行打包脚本（`scripts/package.tcl`），观察综合阶段的报错信息。

#### 4.2.5 小练习与答案

**练习 1**：PSI HDL Library License 和普通 LGPL 的关键区别是什么？为什么对 FPGA 项目特别重要？

> **答案**：多了一条「固件例外」。它把 FPGA 比特流、Flash 镜像都算作「binary」，允许按自己的条款分发。这样用 `fpga_base` 综合出来的闭源 FPGA 产品就不会被 LGPL 的传染性「感染」，对工业产品至关重要。

**练习 2**：README 依赖区块上下有两行 HTML 注释（`<!-- DO NOT CHANGE FORMAT ... -->` 和 `<!-- END OF PARSED SECTION -->`），它们的作用是什么？

> **答案**：它们是给 `scripts/dependencies.py` 用的「区间标记」。解析脚本据此定位需要解析的依赖文本范围。所以不能随便改动这个区块的格式，否则脚本解析会失败。

**练习 3**：PsiIpPackage 这个依赖，普通 GitHub 用户能不能直接克隆到？

> **答案**：按 README 的链接，它托管在 `git.psi.ch`（PSI 内部 Git），社区用户通常无法直接访问；而 psi_common、PsiSim 在 GitHub 上公开可克隆。所以社区开发者拿到 PsiIpPackage 一般要靠仓库分发或其它途径。

---

### 4.3 版本演进历史

#### 4.3.1 概念说明

`Changelog.md` 记录了 `fpga_base` 从 1.0.0 到当前 1.4.0 的全部变化。读懂 changelog 有两个好处：

1. **理解现状的来由**：很多「为什么这么设计」的答案藏在历史里。比如「为什么地址宽度固定 8 位」「为什么 AXI 从机来自 psi_common」，都能在 changelog 找到出处。
2. **建立全局心智模型**：顺着版本号，你能看到这个 IP 是怎么从「只有版本读出」逐步长出 SW 驱动、EPICS 模板、依赖脚本、Python 版本注入这些能力的。

IP 的当前版本号也写死在打包产物 `component.xml` 里（`<spirit:version>1.4</spirit:version>`），与 Changelog 顶端一致。

#### 4.3.2 核心流程

把六个版本按时间从早到晚排，能力是这样「长」出来的：

```text
1.0.0  从 CVS 移植过来（最初的起点）
  |
1.1.0  + 新增软件(SW)驱动；把 AXI 地址范围固定为 8 位；要求 PsiIpPackage>=1.2.0
  |
1.2.0  + 新增 EPICS 模板（接入实验物理控制系统）
  |
1.3.0  + 新增依赖解析脚本；改用 psi_common 的 AXI 从机（替换旧版）
  |
1.3.1  + 补文档；在 GitHub 开源
  |
1.4.0  + 新增「用 Python 脚本更新构建信息」的选项（git hash 注入）
```

可以看到一条清晰的主线：**先有硬件，再加软件驱动，再接 EPICS，再工程化（依赖脚本/开源），最后再增强版本追溯（Python 注入）**。这也正好对应本手册后续几个单元的主题。

#### 4.3.3 源码精读

- 完整 changelog，见 [Changelog.md:1-31](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/Changelog.md#L1-L31)。几个关键版本：

  - **1.0.0**（[Changelog.md:30-31](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/Changelog.md#L30-L31)）：从 CVS 移植——说明这个 IP 早于 Git 时代就存在了。
  - **1.1.0**（[Changelog.md:21-28](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/Changelog.md#L21-L28)）：新增 SW 驱动；并修了一个值得注意的点——**「把 AXI 地址范围固定为 8 位，因为反正其它值也不支持」**。这正是后续讲义会讲到的 `AxiAddrWidth_g=8` 的历史来源。
  - **1.3.0**（[Changelog.md:8-12](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/Changelog.md#L8-L12)）：**改用 psi_common 的 AXI 从机**，替代旧版。这条直接解释了 4.2 节里「为什么 psi_common 不可或缺」。
  - **1.4.0**（[Changelog.md:1-2](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/Changelog.md#L1-L2)）：新增「用 Python 脚本更新构建信息」——对应仓库里的 `scripts/update_version.py`，也是第三单元「版本与编译时间机制」的重点。

- 版本号在产物中的体现，见 [component.xml:6](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L6)——`<spirit:version>1.4</spirit:version>`，与 Changelog 顶端的 1.4.0 对应（IP-XACT 里通常只显示 major.minor）。

#### 4.3.4 代码实践

**实践目标**：用 changelog 解释一个「现状」，体会历史对理解源码的价值。

**操作步骤**：

1. 打开 [Changelog.md:21-28](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/Changelog.md#L21-L28)（1.1.0 条目）。
2. 阅读其中「Made AXI address range constant (8-bits) since other values are not supported anyway」这一句。
3. 在本仓库搜索 AXI 地址宽度的设置（可以用 IDE/Grep 搜 `AxiAddrWidth`），看它是不是确实被写死成了 8（**待后续讲义精读 `hdl/fpga_base_v1_0.vhd` 时确认**，本讲只做「历史 → 现状」的对应练习）。

**需要观察的现象**：changelog 里说「其它值反正也不支持」，所以干脆固定。

**预期结果**：你会理解——很多源码里看起来「硬编码」的常量，其实是有历史决策依据的，读 changelog 能帮你复原这个决策。

**待本地验证**：`AxiAddrWidth` 在 HDL 中的确切写法与取值，请到第二单元「AXI4 从机寄存器接口」再确认。

#### 4.3.5 小练习与答案

**练习 1**：从 1.0.0 到 1.4.0，哪两个版本的变化最「工程化」（即改善了开发流程而非增加硬件功能）？

> **答案**：1.3.0（新增依赖解析脚本）和 1.3.1（补文档 + GitHub 开源）。它们主要改善的是「怎么开发/分发」这个 IP，而不是给硬件加新电路。

**练习 2**：如果你想知道「为什么本 IP 的 AXI 从机来自 psi_common 而不是自己实现」，应该看 Changelog 的哪一条？

> **答案**：看 1.3.0 的 Changes 条目：「Use AXI slave from psi_common instead of legacy version」。这说明 1.3.0 之前用的是自带旧版，之后改用 psi_common。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个小任务：

1. **画一张「项目身份证」**：用一张纸或一个文档，填入以下字段（全部从本讲引用的源码里找，不要凭记忆）：
   - 项目名 / 一句话功能：来自 README `# Description`。
   - 维护者、作者：来自 README 顶部。
   - 许可证名称 + 是否允许闭源 FPGA 镜像分发：来自 README 许可证行 + License.txt 例外条款。
   - 当前版本号：来自 Changelog 顶端 + `component.xml`。
   - 三类依赖清单：来自 README Dependencies 区块。
2. **回答一个判断题**：某公司想用 `fpga_base` 烧进自己的闭源 FPGA 产品里销售，从许可证角度看是否允许？从依赖角度看，他们最少需要拿到哪些库才能把 IP 综合出来？
3. **验证你的依赖图**：对照你画的依赖关系图，把「综合必需」的库圈出来，并写一句话解释为什么是它（提示：1.3.0 的 changelog）。

**参考结论**：

- 许可证：**允许**，因为 PSI HDL License 的固件例外明确把 FPGA bitstream 视为可自由分发的 binary。
- 综合必需的最小集合：**`psi_common`（VHDL 库）**。TCL 类的 PsiIpPackage 等是「重新打包」时才需要的开发期工具，不是电路综合的前提。

这个综合练习把「定位 → 许可证/依赖 → 历史」连成了一条线，帮你建立起对 `fpga_base` 的整体认知，为后续读源码打好地基。

## 6. 本讲小结

- `fpga_base` 是 PSI 的一个 **Vivado IP 核**，提供 FPGA 设计里最常用的基础功能：版本读出、固件/软件编译日期时间、LED、DIP 开关，通过 AXI 寄存器暴露给软件。
- 它采用 **PSI HDL Library License = LGPL 2.1 + 固件例外**，允许综合出的 FPGA bitstream 闭源分发。
- 依赖分三类：**TCL**（PsiSim/PsiIpPackage/PsiUtil，多为开发期）、**VHDL**（`psi_common`，综合必需）、**VivadoIp**（自指），清单写在 README 里且**会被 `scripts/dependencies.py` 解析**，因此格式不能乱改。
- **`psi_common` 不可或缺**，因为 1.3.0 起本 IP 的 AXI 从机就来自它。
- 版本从 1.0.0（CVS 移植）演进到 1.4.0（Python 版本注入），主线是「硬件 → 软件驱动 → EPICS → 工程化 → 版本追溯增强」，恰好对应本手册后续单元。
- 本讲只读「门面文件」，没有碰任何 HDL/电路逻辑；下一讲起才进入目录结构与真正的源码。

## 7. 下一步学习建议

- **接下来读**：[u1-l2 仓库目录结构与文件角色详解](u1-l2-repository-structure.md)——建立整个仓库的「目录地图」，知道 `hdl`、`scripts`、`bd`、`xgui`、`drivers`、`epics` 各放什么。
- **再之后**：[u1-l3 构建与打包流程总览](u1-l3-build-and-packaging-overview.md)——从高层俯瞰「VHDL 源码 → 可分发 IP zip 包」的完整流水线。
- **想深入依赖机制**：精读 [scripts/dependencies.py](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py) 和它依赖的外部 `PsiFpgaLibDependencies` 包。
- **想看完整文档**：直接打开 [doc/fpga_base.pdf](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/doc/fpga_base.pdf) 数据手册。
