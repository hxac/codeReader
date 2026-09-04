# 第 1 讲：ICS55 是什么——开源 55nm PDK 全景与许可

> 本讲是整本学习手册的第一讲，不要求你有任何芯片设计基础。我们把「PDK」这个词彻底讲清楚，再认识 ICS55 这个项目的来历、当前状态，最后弄明白你「能拿它做什么、不能做什么」——这由 Apache-2.0 许可证和 preview 状态共同决定。

---

## 1. 本讲目标

读完本讲，你应该能够：

1. 用自己的话说清楚 **PDK（Process Design Kit，工艺设计套件）** 是什么，以及它在数字后端流程（逻辑综合、布局布线、物理验证）中分别提供哪类文件。
2. 复述 **ICS55 的三方参与主体**（ICsprout、浙江大学集成电路学院、中科院计算所 ECOS Team）、首次发布时间（2025 年 10 月），以及 **preview 状态下的硬性限制：严禁用于任何形式的商业量产流片**。
3. 理解 Apache-2.0 许可证的核心要求：**保留版权/专利/商标/归属声明，修改过的文件必须在文件头注明修改**，并知道仓库如何通过「每个数据文件都带 license header」来落实这一点。
4. 独立完成一次仓库体检：用 `grep` 统计哪些文件带 Apache-2.0 头、哪些不带，并写出一份「能做/不能做」清单。

---

## 2. 前置知识

本讲几乎不需要写代码的基础，但以下几个名词会反复出现，先用大白话解释一遍：

| 名词 | 通俗解释 |
| --- | --- |
| **流片（tapeout）** | 把设计好的电路交给工厂加工成真实芯片。「量产流片」指大规模生产，「测试流片（shuttle/MPW）」指小批量试产。 |
| **代工厂（foundry）** | 只负责制造、不负责设计芯片的工厂。ICsprout 就是一家代工厂。 |
| **工艺节点（node）** | 制造工艺的代际称呼，数字越小一般越先进。ICS55 是 55nm CMOS 工艺。 |
| **视图（view）** | 同一个电路在不同 EDA 工具眼里的「快照」。布线器看 LEF，综合器看 liberty，仿真器看 Verilog，版图工具看 GDS。 |
| **EDA 工具** | 电子设计自动化软件，例如开源的 yosys（综合）、OpenROAD（布局布线）、KLayout（版图查看）。 |
| **许可证（license）** | 一份法律文件，规定你能怎么用、怎么改、怎么再分发这份材料。 |

---

## 3. 本讲源码地图

本讲涉及的关键文件（这个仓库的「源码」就是这些 EDA 数据文件和文档）：

| 文件 | 作用 |
| --- |
| [README.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md) | 项目门面：Todo、下载方式、Introduction、Status、Contents 目录树、许可说明。本讲最主要的精读对象。 |
| [LICENSE](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/LICENSE) | 完整的 Apache-2.0 许可证正文（201 行），法律效力的最终依据。 |
| [CONTRIBUTING.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md) | 贡献指南：DCO 签署、PR 评审流程、行为准则。 |
| [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef) | 工艺 LEF。本讲只看它开头 13 行的 license header，文件本身是第 2 单元的主角。 |
| [IP/IO/.../liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib) | IO 库的 liberty 时序库。本讲同样只看文件头。 |
| [IP/STD_cell/.../verilog/ics55_LLSC_H7CH.v](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v) | 标准单元库的 Verilog 仿真模型。本讲只看文件头。 |
| [IP/STD_cell/.../cdl/ics55_LLSC_H7CH.cdl](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl) | 晶体管级网表。本讲只看文件头。 |
| [IP/STD_cell/.../cell_list/ics55_LLSC_H7CH.txt](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt) | 单元清单，748 行，一行一个单元名。**注意：它没有 license header**，这是个有意思的细节，后面会分析。 |

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

- 4.1 PDK 概念与组成
- 4.2 ICS55 背景与状态
- 4.3 Apache-2.0 许可与逐文件 license header

---

### 4.1 PDK 概念与组成

#### 4.1.1 概念说明

**PDK（Process Design Kit，工艺设计套件）** 是代工厂交给芯片设计者的一包「翻译材料」。

问题的根源在于：设计者用抽象的逻辑描述电路（「一个与门」「一根金属线」），而工厂只能理解具体的物理图形（「这块多晶硅的宽度是 0.1μm」「这层金属间距不能小于 0.14μm」）。两者之间必须有一套标准的「词典 + 语法书」，这就是 PDK。

一个完整的先进工艺 PDK 通常包含：

| PDK 组成 | 给谁用 | 对应文件类型 |
| --- | --- | --- |
| 工艺规则（DRC/LVS deck） | 物理验证工具 | Calibre / KLayout 规则文件 |
| 器件 SPICE 模型 | 电路仿真器 | `.scs` / `.lib`（SPICE 语法） |
| 标准单元库 | 综合 + 布局布线 | liberty（时序）、LEF（抽象）、GDS（版图）、CDL（网表）、Verilog（仿真） |
| IO / PAD 库 | 芯片顶层设计 | 同上，外加 datasheet |
| 布线技术文件 | 布局布线工具 | tech LEF（金属层、过孔、site） |
| 文档 | 人 | user guide、datasheet |

PDK 在数字后端流程各环节的落点：

```
RTL ──► 逻辑综合(yosys) ──► 门级网表 ──► 布局布线(OpenROAD) ──► DEF/GDS ──► 物理验证
              │                                │                              │
        需要 liberty               需要 tech LEF + cell LEF              需要 DRC/LVS 规则
        (单元的时序/面积/功耗)      (金属层怎么走线、单元什么形状)        (检查是否违反工艺规则)
```

**关键认知**：PDK 不是「一个软件」，而是一堆互相配套的数据文件。同一颗单元（比如一个与非门）会同时出现在 liberty、LEF、Verilog、CDL、GDS 五个文件里，各自描述它的一个侧面。这个「多视图」结构是理解本仓库目录组织的钥匙，也是第 5 单元「一致性检查」的伏笔。

#### 4.1.2 核心流程

一个芯片从想法到硅片，PDK 参与的路径可以概括为：

1. 设计者写 RTL（Verilog 硬件描述语言）。
2. **综合工具**读 PDK 的 liberty，把 RTL 翻译成「由具体单元组成的网表」。
3. **布局布线工具**读 tech LEF + cell LEF，把网表里的单元摆到芯片上、连上金属线。
4. **验证工具**用 DRC/LVS 规则检查版图是否合法。
5. **仿真器**用 Verilog 模型 / SPICE 模型验证功能与时序。
6. 通过后交付工厂流片。

任何一步缺了对应的 PDK 文件，流程就断。这也是评价一个开源 PDK「好不好用」的核心标准：**它的文件覆盖了流程的多少环节**。

#### 4.1.3 源码精读

**（a）README 把 PDK 定位说得很直接**

[README.md:37-39](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L37-L39) 这一段是官方定义，划重点：

> The **ICsprout 55nm Open Source PDK** ... provides a complete and production-proven design rule files, device models, standard cell libraries, and parameterized cells. It fully supports the backend physical design flow of digital integrated circuits, including key steps such as **logic synthesis, place and route, and physical verification**, etc.

翻译：官方声称提供了设计规则文件、器件模型、标准单元库和参数化单元，完整支持「综合、布局布线、物理验证」的数字后端流程。**但请注意，这句宣传语和下面 Todo 清单之间有落差**（见 4.1.3(b)），读官方文档时保持这种核对习惯很重要。

**（b）Todo 清单透露了「现在还缺什么」**

[README.md:5-8](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L5-L8) 列出了待补内容：

```
- RAMs, DRC/LVS Rules, SPICE Models, PDN, RC, etc
- User Guide, Tutorials, Datasheets, etc
```

也就是说：**RAM 单元、DRC/LVS 验证规则、SPICE 器件模型、供电网络（PDN）参数、寄生（RC）参数目前都还没有**。对照 4.1.1 的表格你会发现：目前仓库实际能支撑的是「综合 + 布局布线 + 门级仿真」这条链，物理验证环节的规则文件尚未发布。这是使用 ICS55 前必须知道的边界。

**（c）Contents 目录树确认了实际交付物**

[README.md:63-106](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L63-L106) 给出了完整目录树。把它和 4.1.1 的表格对上：

```
├── IP
│   ├── IO
│   │   └── ICsprout_55LLULP1233_IO_251013   # IO 库（23 个 pad 单元）
│   │       ├── cdl / cell_list / doc / gds / lef / liberty / verilog
│   └── STD_cell                             # 标准单元库
│       └── ics55_LLSC_H7C_V1p10C100         # 版本 1.10
│           ├── ics55_LLSC_H7CH              # HVT（高阈值）单元，748 个
│           ├── ics55_LLSC_H7CL              # LVT（低阈值）单元，747 个
│           └── ics55_LLSC_H7CR              # RVT（常规阈值）单元，747 个
└── prtech                                   # 布局布线工艺文件
    └── techLEF                              # tech LEF 在这里
```

三个要点：

1. **每个库目录下都是同一套七个子目录**（cdl/cell_list/doc/gds/lef/liberty/verilog）——这就是 4.1.1 说的「多视图」在磁盘上的直接体现。
2. 注意 **liberty 和 gds 目录在 git 仓库里是空的**：这些文件体积大，通过 GitHub Release 用 `make unzip` 下载，`.gitignore` 把它们排除了（第 1 讲下一节 u1-l3 专门讲这个机制）。
3. `prtech` 只有 tech LEF，没有 DRC/LVS deck——再次印证 Todo 清单。

**（d）cell_list：最小的视图**

[cell_list/ics55_LLSC_H7CH.txt:1-7](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1-L7) 的内容朴素到只有单元名：

```
ADDFX1H7H
ADDFX1P4H7H
ADDFX2H7H
ADDHX1H7H
...
AND2X0P5H7H
```

一行一个单元名，全库 748 行。它是「这个库里到底有哪些单元」的权威清单，后面所有跨视图核对都以它为基准。**同时注意第 1 行直接就是单元名——这个文件没有 license header**，4.3 节会回来讨论这一点。

#### 4.1.4 代码实践

**实践 A：数一数每个库有多少个单元**

1. **实践目标**：用真实数据验证 4.1.3(c) 中的单元数量，建立对仓库规模的直觉。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   wc -l IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt \
         IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt \
         IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt \
         IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/ICSIOA_N55_3P3.txt
   ```

3. **需要观察的现象**：四个数字分别是 748、747、747、23。
4. **预期结果**：H7CH 比 H7CL/H7CR 多 1 个单元（差 1 个，不是 bug，是三套库的功能覆盖略有差异——第 3 单元会解释原因）。IO 库只有 23 个单元，因为它只需覆盖 pad 的几种类型。
5. 本实践已在仓库当前 HEAD（`68d89ed`）验证过结果如上；你本地应得到相同输出。

#### 4.1.5 小练习与答案

**Q1：综合工具和布局布线工具分别需要 PDK 里的哪类文件？为什么不能互换？**

参考答案：综合工具需要 **liberty**（单元的时序、面积、功耗数据），因为它只关心「这个门逻辑上是什么、有多快」，不关心物理形状。布局布线工具需要 **tech LEF**（金属层、间距、site）和 **cell LEF**（单元的外形、引脚位置、障碍区），因为它要把单元真的摆到硅片上并连线。liberty 里没有几何信息，LEF 里没有时序信息，所以两者缺一不可、不能互换。

**Q2：根据 README 的 Todo 清单，目前 ICS55 无法支撑后端流程中的哪个环节？**

参考答案：**物理验证环节**。Todo 里明确列出了 `DRC/LVS Rules` 尚未提供，而 DRC（设计规则检查）和 LVS（版图与原理图一致性检查）正是物理验证的核心。此外 `SPICE Models` 未提供也限制了晶体管级电路仿真。不过 IO 库已带有 datasheet PDF（`doc/` 目录），标准单元的 doc 目录也有典型条件数据手册。

---

### 4.2 ICS55 背景与状态

#### 4.2.1 概念说明

这一节回答三个问题：**谁做的、什么时候发布、现在处于什么状态**。

三个参与主体，分工不同：

| 主体 | 角色 |
| --- | --- |
| **ICsprout Integrated Circuit Co., Ltd.** | 55nm 工艺的**拥有者和制造方**，2021 年由浙江省政府与浙江大学联合发起成立，拥有 12 英寸晶圆中试线，具备 180nm/55nm CMOS、55nm eFlash、180nm BCD 工艺。 |
| **浙江大学集成电路学院**（College of Integrated Circuits, ZJU） | PDK 的**联合开发方**。 |
| **ECOS Team（中科院计算所）** | PDK 的**维护和发布方**。ECOS = EDA + Chip + OneStudentOneChip + System 的首字母，也是 Ecosystem（生态）的前四个字母，由原「一生一芯」团队与原 iEDA 团队合并而成。 |

发布时间线：README 明确写着 **first released in October 2025**；git 仓库的首个提交 `3338e16`（`feat: add open source pdk`）日期为 **2025-10-21**，两者互相印证。

**为什么这件事在开源芯片圈是大事**：在 ICS55 之前，可用的开源 PDK 主要是 SkyWater 130nm、GlobalFoundries 180nm、IHP 130nm（SG13G2）。55nm 是当时开源世界里最先进的工艺节点，意味着更高的集成度与更低的功耗。

#### 4.2.2 核心流程

ICS55 当前的生命周期状态可以用一个简单的状态机描述：

```
[开发验证 + 迭代优化]  ──(计划 2025-12 首次工程批测试流片)──►  [取得流片数据]
        ▲                                                              │
        └────────────── 根据流片数据继续修复优化 ◄─────────────────────┘
                                │
                                ▼
                  [仅支持小批量测试流片（人才培养/科研）]
                  [商业量产流片：明确禁止，且未来多次流片成功也不保证放开]
```

注意这是一个**长期单向收敛但不开闸**的状态：即使将来多次流片成功，官方也没有承诺会进入商业量产支持。

#### 4.2.3 源码精读

**（a）三方主体与首次发布时间的官方表述**

[README.md:39](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L39) 一句话交代了全部主体：

> ...independently developed by **ICsprout Integrated Circuit Co., Ltd.** and **College of Integrated Circuits Zhejiang University**, maintained and released (**first released in October 2025**) by **ECOS Team, Institute of Computing Technology, Chinese Academy of Sciences**.

**（b）preview 状态的硬性限制**

[README.md:56-61](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L56-L61) 是全仓库最需要划红线的一段。README 用 GitHub 的 `[!WARNING]` 语法突出显示：

> `ICsprout currently offers the PDK contents as a preview release only!`

紧接着正文给出三条限制，逐条翻译：

1. 「ICS55 目前仍处于开发验证和迭代优化阶段，尚未通过完整的硅片测试与可靠性认证。**因此严禁用于任何形式的商业量产流片项目！**」
2. 「根据 ICsprout 与 ECOS Team 的内部决定，ICS55 的首次工程批测试流片（shuttle）暂定于 2025 年 12 月。」
3. 「**需要特别注意的是，即使未来多次成功流片，也不保证该 PDK 已达到商业量产标准。**在可预见的未来，ICS55 仅支持面向人才培养和学术研究的小批量测试流片。」

**（c）ICsprout 公司背景**

[README.md:110-112](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L110-L112)：ICsprout 由浙江省政府和浙江大学于 2021 年共同创立，依托 12 英寸晶圆中试线，工艺组合覆盖 180nm 与 55nm CMOS，以及 55nm 嵌入式 Flash（eFlash）和 180nm BCD 等特色工艺。

**（d）ECOS Team 的定位**

[README.md:114-116](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L114-L116)：团队名称取自 **E**DA、**C**hip、**O**neStudentOneChip、**S**ystem 的首字母，同时是 **Ecos**ystem 的前四个字母。成员主要来自中科院计算所前沿系统实验室与北京开源芯片研究院，愿景是「用开源降低芯片设计门槛，赋能千行百业」。**这也是仓库里大量 `_ecos` 后缀文件的由来**——那些是 ECOS Team 为适配开源 EDA 工具而维护的平行版本，第 2、3、6 单元会反复遇到。

**（e）用 git 历史交叉验证发布时间**

只读 git 命令即可验证（不是编造）：

```bash
git log --reverse --format="%h %ad %s" --date=short | head -3
# 3338e16 2025-10-21 feat: add open source pdk
```

首个提交 `feat: add open source pdk` 落在 2025 年 10 月 21 日，与 README 的「first released in October 2025」一致。

#### 4.2.4 代码实践

**实践 B：从 README 抽取参与方和联系渠道**

1. **实践目标**：熟悉用「问题驱动」的方式读官方文档，而不是从头到尾通读。
2. **操作步骤**：

   ```bash
   # 找到所有参与主体段落
   grep -n "^###" README.md
   # 找到联系邮箱
   grep -n "@" README.md
   ```

3. **需要观察的现象**：`grep -n "^###"` 输出 `### ICsprout` 与 `### ECOS Team` 两个小节标题；邮箱搜索命中 `ecos-all@ict.ac.cn`。
4. **预期结果**：对照 [README.md:140-145](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L140-L145) 的联系表，ECOS Team 的角色标注为「Project management, Document maintenance」。注意表中**没有** ICsprout 的直连邮箱——技术问题走 ECOS Team 是官方指定渠道。
5. 本实践已在仓库当前 HEAD 验证。

#### 4.2.5 小练习与答案

**Q1：如果你所在的公司想用 ICS55 做一颗商用芯片并量产，可以吗？**

参考答案：**不可以**。README 的 Status 一节用加粗+警告框明确写了「strictly prohibited for use in any form of commercial mass production tapeout projects」。而且这不是暂时的技术限制——官方进一步说明即使未来多次流片成功，也不保证达到商业量产标准。商用必须另寻正式授权的商用 PDK。

**Q2：`_ecos` 后缀的文件（例如 `N551P6M_ecos.lef`）是谁维护的、为什么存在？**

参考答案：由 ECOS Team 维护。它们是为开源 EDA 工具链适配的平行版本，官方 README 也提到 ECOS Team 正致力于提升 ICS55 与主流开源 EDA 工具链的兼容性和稳定性。具体改了什么（补 RC 参数、补电源引脚、定义 site 等）会在 u2-l3、u3-l3、u6-l3 三讲中用 diff 逐一拆解。

**Q3：用哪条 git 命令验证 ICS55 的首次发布时间？**

参考答案：`git log --reverse --format="%h %ad %s" --date=short | head -3`，把提交按时间正序排列后看最早一条，得到 `3338e16 2025-10-21 feat: add open source pdk`，与 README 声明的 2025 年 10 月一致。

---

### 4.3 Apache-2.0 许可与逐文件 license header

#### 4.3.1 概念说明

**许可证解决的问题是**：这包文件的法律身份是什么？你能用、能改、能再分发吗？能拿去做闭源商业产品吗？

ICS55 采用 **Apache-2.0**。README 里用一句话概括了它的性格（[README.md:122](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L122)）：

> 允许用户修改代码，并以闭源或开源产品的形式使用（即**商业友好**）；但要求在分发软件时**保留原代码中的版权、专利、商标和归属声明**，并且**在被修改文件的头部注释中标明修改**。

对比一下常见的几种开源许可证，能看出这个选择的意图：

| 许可证 | 商业友好 | 传染性（改了就必须开源） | 专利授权条款 |
| --- | --- | --- | --- |
| Apache-2.0 | ✅ 高 | ❌ 无 | ✅ 明确授予 |
| MIT | ✅ 高 | ❌ 无 | ⚠️ 未明确 |
| GPL-3.0 | ⚠️ 受限 | ✅ 有 | ✅ 明确授予 |

对一个希望被工业界放心使用的 PDK 来说，Apache-2.0 是稳妥选择：你可以在闭源商业工具链里引用它，不用担心「传染」，同时它明确授予专利使用权（MIT 没写），比 MIT 更适合硬件场景。

**「逐文件 header」是本仓库的一个特色做法**。Apache-2.0 只要求「分发时随附许可证副本」，并不强制每个文件都嵌一段声明。但 ICS55 的实体是**数据文件**（LEF、liberty、CDL），不是代码仓库——数据文件被单独抽出、嵌入工具、转换格式的概率远高于普通代码，一旦脱离仓库就失去了 `LICENSE` 文件的保护伞。所以在每个数据文件头部都放一份声明，等于给每个文件随身携带「法律身份证」。

#### 4.3.2 核心流程

Apache-2.0 第 4 条（Redistribution，再分发）规定了四个条件，这是你使用 ICS55 时真正会触碰到的条款：

```
你想再分发 ICS55（原样或修改后）
    │
    ├─ (a) 必须向接收者提供一份 Apache-2.0 许可证副本
    ├─ (b) 修改过的文件必须带上"你改了这个文件"的显著声明
    ├─ (c) 必须保留源文件中的版权/专利/商标/归属声明
    └─ (d) 若有 NOTICE 文件，需在衍生品中保留其中的归属说明
    │
    ▼
附带条款：第 7 条免责声明（不提供任何担保）、第 8 条责任限制
```

落实到日常操作，就是两条硬性动作：

1. **不改文件、只是使用** → 不需要做什么，但若把文件发给别人，请附上 LICENSE。
2. **改了文件**（比如自己生成一版 `_myflow.lef`）→ 必须在新文件头部写明「本文件基于 ICS55 修改」，并保留原 Apache-2.0 声明。第 6 单元 u6-l3 会带你实际演练一次。

#### 4.3.3 源码精读

**（a）LICENSE 第 4 条：再分发的四个条件**

[LICENSE:89-104](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/LICENSE#L89-L104) 是标准 Apache-2.0 第 4 条，条件 (b) 和 (c) 与我们最相关：

> (b) You must cause any modified files to carry prominent notices stating that You changed the files; and
>
> (c) You must retain, in the Source form of any Derivative Works that You distribute, all copyright, patent, trademark, and attribution notices from the Source form of the Work...

[LICENSE:143](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/LICENSE#L143) 的第 7 条则声明「按原样提供，不提供任何明示或默示担保」——法律上把 PDK 数据出错的风险留在使用者一侧。

**（b）LICENSE 附录：官方推荐的标准 header 模板**

[LICENSE:178-201](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/LICENSE#L178-L201) 给出了「如何把 Apache 许可证应用到你的作品」的样板，其中版权行已经填好了 `Copyright 2025 ICsprout Integrated Circuit Co., Ltd.`。**仓库里每个数据文件头部的声明，就是这段模板原样嵌入的结果。**

**（c）同一个声明，四种注释语法**

这是本讲最值得动手看的细节。同一段 13 行的 Apache-2.0 声明，在不同格式文件里用了该格式各自的注释前缀：

| 文件 | 注释前缀 | 源码位置 |
| --- | --- | --- |
| tech LEF（`.lef`） | `#` | [N551P6M.lef:1-13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L1-L13) |
| liberty（`.lib`） | `/* ... */` | [ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib:1-15](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib#L1-L15) |
| Verilog（`.v`） | `/* ... */` | [ics55_LLSC_H7CH.v:1-15](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L1-L15) |
| CDL 网表（`.cdl`） | `*` | [ics55_LLSC_H7CH.cdl:1-13](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl#L1-L13) |

例如 liberty 文件的开头是 C 风格块注释：

```
/*
 * Copyright 2025 ICsprout Integrated Circuit Co., Ltd.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 ...
 */
```

**为什么要换注释符？** 因为注释写错会破坏文件本身：LEF 的注释符是 `#`，如果你把 `/* */` 塞进 LEF，解析器会直接报语法错误。这提示我们一个通用事实：**给 EDA 数据文件加任何内容都必须遵守该格式的语法**。你在第 6 单元做二次开发生成新视图文件时，同样要选对注释前缀。

**（d）哪些文件没有 header？——诚实的边界**

用 grep 统计（见 4.3.4 实践 C），仓库 41 个被 git 跟踪的文件中，**29 个带 Apache-2.0 header，12 个不带**。不带的是：

- 4 个 `cell_list/*.txt`（纯单元名清单）
- 4 个 `doc/*.pdf`（厂商数据手册，PDF 无法嵌文本注释，且版权属原厂）
- `.gitignore`、`Makefile`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`（工程与社区元文件）

所以 README 里「each source file in ICS55 contains an Apache 2.0 license header」的说法应理解为**针对 PDK 的设计数据文件**（LEF/liberty/CDL/Verilog/GDS），而不是字面上的每一个文件。PDF 不带的原因很直接：二进制格式没法插注释，它们本来就是外部文档。

**（e）贡献流程：DCO 签署**

[CONTRIBUTING.md:6-14](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md#L6-L14) 要求每次贡献附带 **DCO（Developer Certificate of Origin，开发者来源证明）**，即提交时加 `git commit -s` 生成的 `Signed-off-by:` 行，表示你确认自己有权提交这段内容。[CONTRIBUTING.md:16-20](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/CONTRIBUTING.md#L16-L20) 说明所有提交（包括项目成员的）都必须走 GitHub Pull Request 评审。

#### 4.3.4 代码实践

**实践 C：统计仓库的 license header 覆盖率（本讲主实践之一）**

1. **实践目标**：验证 4.3.3(d) 的结论「29/41 文件带 header」，并亲手找出那 12 个不带的文件。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   # 1) 列出被 git 跟踪的文件总数
   git ls-files | wc -l

   # 2) 列出【不包含】Apache 声明的被跟踪文件（-L 表示列出不含匹配的文件）
   git ls-files | xargs grep -L "Licensed under the Apache License"

   # 3) 数一数带声明的文件数
   git ls-files | xargs grep -l "Licensed under the Apache License" | wc -l

   # 4) 观察同一份声明在不同格式里的注释前缀差异
   head -3 prtech/techLEF/N551P6M.lef
   head -3 IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib
   head -3 IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cdl/ics55_LLSC_H7CH.cdl
   ```

3. **需要观察的现象**：
   - 第 1 条命令输出 `41`。
   - 第 2 条命令输出 12 行，全部是 `cell_list/*.txt`、`doc/*.pdf`、`.gitignore`、`Makefile`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`（注意 `README.md` 与 `LICENSE` 本身**带**匹配文本，所以不在这个列表里）。
   - 第 3 条命令输出 `29`。
   - 第 4 条命令分别显示 `#`、`/*`、`*` 三种注释开头。
4. **预期结果**：41 = 29 + 12，与 4.3.3(d) 的结论一致。
5. 上述数字已在仓库当前 HEAD（`68d89ed`）实际执行验证。若你使用了 `make unzip` 下载了 liberty/gds，`git ls-files` 不会列出它们（被 `.gitignore` 排除），统计结果不变。

#### 4.3.5 小练习与答案

**Q1：Apache-2.0 相比 MIT，对这个 PDK 项目多提供了什么？**

参考答案：主要有两点。一是**明确的专利授权条款**（LICENSE 第 3 条），对涉及硬件工艺和器件结构的 PDK 尤为重要，使用者不必担心隐性专利风险；二是**修改声明义务**（第 4 条 (b)），要求改动必须在文件头注明，这对需要区分「原厂数据」和「社区适配数据」的 PDK 场景（例如 `_ecos` 系列）提供了法律上的可追溯性。两者都保留「商业友好、无传染性」的特点。

**Q2：你想基于 `N551P6M.lef` 生成一份自己改过的 tech LEF，需要遵守什么？**

参考答案：至少三点：(1) 保留原文件的版权与 Apache-2.0 声明（第 4 条 (c)）；(2) 在新文件头部显著注明你修改了该文件、改了什么（第 4 条 (b)）；(3) 若把文件分发给别人，需随附一份 Apache-2.0 许可证副本（第 4 条 (a)）。另外技术上要记得用 `#` 作为注释前缀，否则 LEF 解析器会报错。

**Q3：为什么 `cell_list/*.txt` 和 `doc/*.pdf` 没有 license header，这算违规吗？**

参考答案：不算。Apache-2.0 的合规要求是「分发时随附许可证」，并不强制每个文件内嵌声明。`cell_list` 是一行一个单元名的纯清单，嵌入注释反而可能干扰自动化脚本逐行解析；PDF 是二进制文档，无法插入文本注释，其版权由原厂数据手册自身约束。仓库的核心设计数据文件（LEF/liberty/CDL/Verilog/GDS）全部带 header，已满足「每个组件文件都有清晰许可指引」的初衷。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份**《ICS55 使用边界报告》**。这是本讲的主实践任务。

### 5.1 实践目标

产出一份不超过 400 字的报告 + 一张目录树，回答「我能用这个 PDK 做什么 / 不能做什么」，全部结论必须有源码依据（文件 + 行号），不允许凭印象写。

### 5.2 操作步骤

**第 1 步：克隆并通读 README**

```bash
git clone https://github.com/openecos-projects/icsprout55-pdk
cd icsprout55-pdk
less README.md     # 重点读 Todo / Usage / Introduction / Status / Contents 五节
```

**第 2 步：提取参与方、Todo、目录树**

```bash
# 参与方与联系方式
grep -n "ICsprout\|Zhejiang\|ECOS" README.md | head -20

# Todo 清单（README 第 5-8 行）
sed -n '5,8p' README.md

# Contents 目录树（README 第 67-106 行）
sed -n '67,106p' README.md
```

把目录树誊抄/重画进你的报告，并给每个子目录标注它对应哪种 EDA 视图（提示：对照本讲 4.1.1 的表格；`cdl` 是晶体管网表、`lef` 是物理抽象、`liberty` 是时序、`verilog` 是仿真模型、`gds` 是版图、`doc` 是数据手册）。

**第 3 步：确认许可与限制**

```bash
# 找到 WARNING 框和限制条款
grep -n "WARNING\|prohibited\|commercial" README.md
# 看 LICENSE 第 4 条再分发条件
sed -n '89,104p' LICENSE
```

**第 4 步：执行本讲 4.3.4 的实践 C**，把 29/41 的统计结果和 12 个不带 header 的文件清单附进报告。

**第 5 步：撰写结论**，按下面模板写 200 字左右的小结：

> 我能用 ICS55：______（提示：学习数字后端流程、做综合与布局布线练习、小批量科研/教学测试流片、以闭源或开源产品形式使用并修改，前提是保留版权声明并注明修改）
>
> 我不能用 ICS55：______（提示：任何形式的商业量产流片；在未提供 DRC/LVS 规则与 SPICE 模型前，不能做完整的物理验证和晶体管级仿真）

### 5.3 需要观察的现象

- `sed -n '67,106p' README.md` 输出的目录树中，三个标准单元库目录名分别是 `ics55_LLSC_H7CH/H7CL/H7CR`，对应 HVT/LVT/RVT 三种阈值电压（这是第 3 单元的主题，现在只需记住名字）。
- `grep -n "prohibited" README.md` 会命中第 61 行的加粗禁令。

### 5.4 预期结果

一份包含三部分的报告：① 参与方表（三方主体 + 角色 + 联系邮箱）；② 标注了视图类型的目录树；③ 200 字「能做/不能做」小结。**提示**：本仓库不提供任何商业量产许可路径——如果你的报告里出现「可以量产」，请回到 [README.md:56-61](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L56-L61) 重读一遍。

> 说明：第 1、4 步中的 grep/sed 统计命令与数字均已在仓库 HEAD `68d89ed` 下实际执行验证；克隆与 `less` 等交互式步骤需要你本地完成。

---

## 6. 本讲小结

- **PDK 是代工厂与设计者之间的「词典」**：一个 PDK 不是软件，而是一组互相配套的多视图数据文件——liberty 给综合、LEF 给布局布线、CDL/Verilog 给仿真、GDS 给版图、DRC/LVS 规则给物理验证。
- **ICS55 由三方共建**：ICsprout（工艺与制造，2021 年成立，12 英寸线，55nm CMOS）、浙大集成电路学院（联合开发）、中科院计算所 ECOS Team（维护与发布）；git 首提交 `3338e16`（2025-10-21）与 README 的「2025 年 10 月首发」互相印证。
- **preview 状态 = 硬性红线**：严禁任何形式的商业量产流片，且官方明确即使未来多次流片成功也不保证放开；可预见的未来只支持人才培养与科研的小批量测试流片。
- **当前能力边界由 Todo 决定**：RAM、DRC/LVS 规则、SPICE 模型、PDN、RC 尚未提供，因此「综合 + 布局布线 + 门级仿真」是当前可完整走通的链路。
- **Apache-2.0 商业友好但有义务**：允许闭源使用与修改，但分发时须保留版权/专利/商标/归属声明，被修改文件须在头部注明修改。
- **逐文件 license header 是本仓库的合规特色**：41 个被跟踪文件中 29 个带 header；同一段声明在 LEF（`#`）、liberty/Verilog（`/* */`）、CDL（`*`）里用不同注释符书写——给 EDA 数据文件加内容必须先懂它的语法。

---

## 7. 下一步学习建议

**下一讲（u1-l2）**：`仓库目录结构与多视图文件族`。我们将走出 README 的文字描述，直接在磁盘上盘点这 41 个文件，写脚本生成一张「视图 × 库」的可用性矩阵，弄清哪些文件在 git 里、哪些必须用 `make unzip` 下载。

**再往后（u1-l3）**：`Makefile 与大文件分发机制`，解析 `patsubst` 模式规则如何把 GitHub Release 上的 `tar.bz2` 包解压到正确的库目录。

**推荐的提前阅读**（可选，非必需）：

- [README.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md) 的 Usage 一节（第 10-35 行），先跑一次 `make -n unzip` 看看会下载什么。
- 随手打开 [prtech/techLEF/N551P6M.lef](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef) 滚动浏览一下，混个眼熟即可——第 2 单元我们会逐层精读它。
- 如果你想了解开源 PDK 的同行：SkyWater SKY130、IHP SG13G2，README 第 120 行也向这些先行者致谢，了解它们有助于理解 ICS55 在开源生态中的位置。
