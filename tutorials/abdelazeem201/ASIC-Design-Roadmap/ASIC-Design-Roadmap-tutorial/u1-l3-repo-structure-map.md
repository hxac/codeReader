# 仓库目录结构与学习资源地图

## 1. 本讲目标

前两讲我们已经知道了「这个仓库是什么」(u1-l1)和「ASIC 设计从 RTL 到 GDSII 的主流程是什么」(u1-l2)。本讲不再讲概念，只解决一个非常实际的问题：

> 当你 `git clone` 下来这个仓库后，面对几十个文件夹和文件，**我该先打开哪一个？某个知识点去哪里找？**

学完本讲，你应该能够：

1. 说出仓库**顶层每个目录/文件**是做什么的，并知道它对应设计流程的哪一环。
2. 区分三类内容：**可读的 EDA 脚本**(Tcl/Perl)、**RTL 设计样例**(Verilog)、**参考资料**(PDF / 图 / 在线链接)。
3. 看懂 `README.md` 的目录式导航结构，并发现其中存在的几处**链接小问题**，学会自己核对。
4. 理解 `.github/workflows/` 里那份 CI 配置**目前并没有真正做构建**，避免对它产生误解。

一句话定位：本讲是这本学习手册的**「索引页」**，后面所有讲义都会反复回到这里的某个目录取材。

---

## 2. 前置知识

本讲默认你已经读过 u1-l1、u1-l2。这里只需再补三个最浅的背景：

- **目录树(tree)**：用缩进表示「谁包含谁」的一种画法，根目录 `ASIC-Design-Roadmap/` 在最顶层，下面每个缩进代表一层子目录。
- **文件后缀**：`.tcl` 是 Synopsys/Mentor 等 EDA 工具的脚本(Tcl 语言)；`.pl` 是 Perl 脚本；`.v` 是 Verilog 源码；`.upf` 是电源意图文件；`.pdf` 是手册；`.jpeg` 是图片；`.zip` 是压缩包；`.tdf`/`.ngc` 是工具特定的数据文件。
- **GitHub 永久链接(permalink)**：形如 `.../blob/<commit哈希>/<路径>#L起始-L结束` 的网址，能精确指向某次提交、某个文件的某些行。本讲所有引用都用当前 HEAD `795d32a`，保证链接不会因为后续提交而失效。

> 小提示：U2 才正式讲 Verilog，U3 才正式讲库文件，本讲**只是让你认识这些文件住在哪个房间**，不要求看懂它们的内容。

---

## 3. 本讲源码地图

本讲「阅读」的对象不是某一段算法，而是**整个仓库的文件布局**。下表是本讲会反复提到的关键路径(全部为仓库中真实存在的文件)：

| 路径 | 类型 | 在本讲中的作用 |
|------|------|----------------|
| `README.md` | 文档 | 仓库的「总目录」，导航所有内容 |
| `Figures/Fig. Complex ASIC Design.jpeg` | 图片 | 仓库唯一的配图，流程示意 |
| `.github/workflows/blank.yml` | CI 配置 | GitHub Actions 工作流(起步模板) |
| `LICENSE` | 文档 | MIT 许可证(u1-l1 已讲) |
| `IC Compiler II/`、`IC Compiler/`、`PrimeTime/`、`mentor_scripts/` | 脚本目录 | 各家 EDA 工具的流程脚本 |
| `MY-Design/`、`cmsdk/`、`memories/` | RTL 样例 | 可读的 Verilog 设计 |
| `Guide to HDL Coding Styles for Synthesis/`、`HDL Compiler for Verilog Reference Manual/`、`yosys_manual.pdf` | PDF 资料 | 参考手册 |

---

## 4. 核心概念与源码讲解

### 4.1 顶层目录划分：按「工具 / 领域」分房间

#### 4.1.1 概念说明

一个仓库如果只有一层扁平的文件列表，找东西会非常痛苦。这个仓库的做法是：**按 EDA 工具或知识领域，把相关文件放进同一个文件夹**。于是出现了两类组织线索：

- **按工具分**：`IC Compiler II/`、`IC Compiler/`、`PrimeTime/`、`mentor_scripts/` 分别对应四套不同厂商/版本的流程脚本。
- **按内容性质分**：`MY-Design/`、`cmsdk/`、`memories/` 放 RTL；`Guide to HDL.../`、`HDL Compiler.../` 放 PDF 手册。

理解这条规律后，你只要问自己「我现在想看的是哪个工具/哪类资料」，就能直接跳进对应目录，而不用满仓库乱翻。

#### 4.1.2 核心流程

把仓库布局和 u1-l2 的设计主流程对照着看，导航逻辑就清晰了：

```
设计阶段                →  仓库里去哪个目录取材
─────────────────────────────────────────────
看路线图/总览            →  README.md
前端 RTL 设计            →  MY-Design/、cmsdk/
时序约束 SDC            →  MY-Design/My_Design.cons
库/物理数据准备          →  IC Compiler II/NDM_Creation.tcl、LEF2FRAM/
ICC2 物理设计(PnR)      →  IC Compiler II/PnR.tcl
旧版 ICC / Mentor 对比   →  IC Compiler/、mentor_scripts/
静态时序签核(STA)        →  PrimeTime/
低功耗(UPF)             →  low_power.upf
自动化脚本              →  IC Compiler II/NDR_rule.pl、Logo.pl
开源综合(yosys)         →  yosys_manual.pdf
```

#### 4.1.3 源码精读

仓库的「总目录」写在 README 里。先看它的目录式导航：

[README.md:L40-L47](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L40-L47) —— 这是 `## 📘 Table of Contents`(目录)那一节，列出五个大块：Introduction、Fundamentals、ASIC Design Flow、Awesome Digital IC Resources、Project Repositories and IPs。这是 README **文字内容**的导航。

而仓库**物理目录**的导航并没有单独写成一张表——它就是你在文件管理器里看到的顶层文件夹。下面这张「带注释的目录树」是本讲根据真实文件整理出来的(详见第 5 节综合实践)：

```
ASIC-Design-Roadmap/                      ← 仓库根
├── README.md                  # 路线图主文档(目录式)
├── LICENSE                    # MIT 许可证
├── .github/workflows/blank.yml # GitHub Actions CI(起步模板)
├── Figures/                   # 仓库配图
├── IC Compiler II/            # ★ 当前主流程 ICC2(PnR.tcl 等)
│   └── Scripts/               #   setup / MCMM 脚本
├── IC Compiler/               # 旧版 ICC 传统流程(对比用)
├── PrimeTime/                 # 静态时序分析 STA
├── mentor_scripts/            # Mentor Nitro 参考流程
├── LEF2FRAM/                  # LEF→FRAM 层映射工具(Perl/Tcl)
├── MY-Design/                 # 最小 RTL 设计样例
├── cmsdk/                     # ARM Cortex-M0 SoC 示例
├── memories/                  # RAM/ROM 模型
├── Guide to HDL Coding Styles for Synthesis/  # 可综合 HDL 规范(PDF)
├── HDL Compiler for Verilog Reference Manual/ # HDL Compiler 手册(PDF)
├── good online sites and books for learning ASIC Design/ # 在线资源清单
├── low_power.upf              # 低功耗电源意图
├── Logo.pl                    # SKILL 版图脚本(BMP 转矩形)
├── lef_layer_tf_number_mapper.pl # 根目录脚本(与 LEF2FRAM/ 下同名文件重复)
├── inputs_for_primeTime.zip   # 给 PrimeTime 的输入打包(二进制)
└── yosys_manual.pdf           # 开源综合器 yosys 手册
```

> 注意两个容易踩坑的点(都用 `git ls-files` 核实过)：
> 1. 根目录的 `lef_layer_tf_number_mapper.pl` 和 `LEF2FRAM/lef_layer_tf_number_mapper.pl` 是**内容完全相同的副本**(`diff` 比较为 IDENTICAL)，看哪个都行，但记得别误以为是两份不同实现。
> 2. README 在第 145 行写了一个指向 `./Tutorials and Courses/README.md` 的链接，但**该目录在仓库中并不存在**(链接已失效)。遇到这种链接，用 `git ls-files` 或 `ls` 自己核对最稳妥。

#### 4.1.4 代码实践

**实践目标**：把上面的「带注释目录树」变成你自己亲手核对过的版本。

**操作步骤**：

1. 在仓库根目录执行只读命令 `git ls-files`(它会列出所有被 git 跟踪的文件，本仓库共 70 个)。
2. 执行 `ls -la` 看顶层文件夹。
3. 逐个对照 4.1.3 的目录树，确认每个目录确实存在、且里面确实有我列出的文件。

**需要观察的现象**：顶层应能看到 13 个目录加若干散落文件；`IC Compiler II/` 下应能数到 5 个 `.tcl/.pl` 文件外加一个 `Scripts/` 子目录。

**预期结果**：你能不假思索地说出「我想看 PnR 主流程 → 进 `IC Compiler II/PnR.tcl`」「我想看 RTL 样例 → 进 `MY-Design/`」。若发现某条目录描述与实际不符，请以实际文件为准——本讲的树是按照 HEAD `795d32a` 整理的。

#### 4.1.5 小练习与答案

- **练习 1**：仓库里出现了几次「ICC」相关目录？它们有什么不同？
  - **答案**：两次——`IC Compiler II/`(新版 ICC2，是当前主流程)和 `IC Compiler/`(旧版 ICC，传统流程)。前者是后续 U4 的主角，后者在 U5 用于横向对比。
- **练习 2**：根目录既有 `lef_layer_tf_number_mapper.pl`，`LEF2FRAM/` 下又有同名文件，哪个才是「正版」？
  - **答案**：两者内容完全相同(`diff -q` 显示 IDENTICAL)，是同一份脚本的重复存放。阅读时任选其一即可，后续讲义统一引用 `LEF2FRAM/` 下的版本。

---

### 4.2 文档与 PDF 参考资料：手册们住在哪里

#### 4.2.1 概念说明

学 ASIC 不可能只靠脚本，还需要大量**官方手册**作为字典。这个仓库贴心地把几类手册以 PDF 形式打包进来了，分目录存放。这部分内容**不需要你读懂**，只需要你知道「想查某个语法/命令时，去哪个 PDF 翻」。

#### 4.2.2 核心流程

参考资料大致分四类，各有归宿：

```
PDF/手册类
├── HDL 编码规范   →  Guide to HDL Coding Styles for Synthesis/   (synco_1..5.pdf)
├── HDL Compiler   →  HDL Compiler for Verilog Reference Manual/  (hdlcv_1..b.pdf，共12个分册)
├── 开源综合器     →  yosys_manual.pdf                            (单文件，约1.1MB)
└── 在线资源清单   →  good online sites and books for learning ASIC Design/README
```

> 术语：**HDL Compiler** 是 Synopsys 把 Verilog 翻译成内部中间表示的前端工具，它的手册之所以拆成 `hdlcv_1.pdf ~ hdlcv_b.pdf` 十几个分册，是因为原始手册太厚，按章节切分便于逐章阅读。

#### 4.2.3 源码精读

每个 PDF 目录里通常配了一个 `ReadMe` 说明该目录的来历。例如可综合 HDL 规范目录的说明文件：

[Guide to HDL Coding Styles for Synthesis/ReadMe](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Guide%20to%20HDL%20Coding%20Styles%20for%20Synthesis/ReadMe) —— 这个目录下放着 `synco_1.pdf` 到 `synco_5.pdf`，是 Synopsys 关于「什么样的 RTL 才能被综合工具正确处理」的编码规范。

[HDL Compiler for Verilog Reference Manual/ReadMe](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/HDL%20Compiler%20for%20Verilog%20Reference%20Manual/ReadMe) —— 旁边这个目录放的是 HDL Compiler 的官方手册，文件名带十六进制序号(`hdlcv_1.pdf`…`hdlcv_a.pdf`、`hdlcv_b.pdf`)，表明它是按原书章节切分的分册。

至于在线资源，仓库专门留了一个 README 收集外部链接：

[good online sites and books for learning ASIC Design/README](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/good%20online%20sites%20and%20books%20for%20learning%20ASIC%20Design/README) —— 这是「书签式」资料，指向仓库之外的网站和书目。

#### 4.2.4 代码实践

**实践目标**：建立「我要查 X，去翻哪个 PDF」的肌肉记忆。

**操作步骤**：

1. 在 `HDL Compiler for Verilog Reference Manual/` 下执行 `ls`，数清一共有多少个 PDF(应为 12 个 `hdlcv_*.pdf`)。
2. 随便打开 `synco_1.pdf` 的第一页(用任意 PDF 阅读器)，确认它是 Synopsys 的 HDL 编码风格文档。
3. 打开 `yosys_manual.pdf` 的目录页，确认它是开源综合器 yosys 的手册。

**需要观察的现象**：PDF 是按章节切分的、有清晰序号；每个 PDF 目录都有一个 `ReadMe` 文件。

**预期结果**：你能回答「可综合 RTL 编码规范在哪？」→ `Guide to HDL Coding Styles for Synthesis/`。

> 这些 PDF 多为商业工具的版权手册，仓库仅用于学习参考，请遵守各手册自身的版权声明。**待本地验证**：受版权限制，本讲不展示 PDF 内文，请自行在本地打开查看目录。

#### 4.2.5 小练习与答案

- **练习 1**：`hdlcv_a.pdf`、`hdlcv_b.pdf` 这种带字母的命名说明了什么？
  - **答案**：原始手册章节超过 9 章，序号用到了十六进制的 `a`、`b`(即第 10、11 分册)，所以是按章节顺序切分的分册命名。
- **练习 2**：想了解开源综合器 yosys，应该打开哪个文件？
  - **答案**：根目录的 `yosys_manual.pdf`(单文件，约 1.1MB)。

---

### 4.3 图与 RTL 示例：从「看图」到「读码」

#### 4.3.1 概念说明

仓库有两类「非文字」的学习材料：一是**配图**，帮你建立全局画面；二是**可读的 Verilog 设计样例**，让你看到真实的 RTL 长什么样。本节把它们放一起，因为它们都是「给你看、给你模仿」的素材，而不是要运行的生产脚本。

#### 4.3.2 核心流程

```
配图：Figures/Fig. Complex ASIC Design.jpeg   ← README 顶部引用的流程示意图
RTL 样例(从简到繁)：
  MY-Design/MY_DESIGN.v        ← 极简教学设计(首选入门)
  memories/cmsdk_ahb_ram.v     ← 单个存储模型
  cmsdk/cmsdk_mcu_system_zed.v ← 真实 SoC 顶层(ARM Cortex-M0)
```

#### 4.3.3 源码精读

README 在开头介绍完「从 RTL 到 GDSII 的旅程」之后，紧接着嵌入了一张图：

[README.md:L20](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L20) —— 这一行用 Markdown 图片语法 `![Complex ASIC Design](...)` 插入配图。

> ⚠️ **值得注意**：这一行里的图片网址指向的是**另一个仓库** `ASIC-Physical-Design-Roadmap`(注意名字多了 `Physical-`)，而本仓库实际叫 `ASIC-Design-Roadmap`。图片的真实本地副本在：

[Figures/Fig. Complex ASIC Design.jpeg](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Figures/Fig.%20Complex%20ASIC%20Design.jpeg) —— 这是仓库唯一的配图(约 597KB 的 JPEG)，紧挨在 README 开篇介绍之后，用于直观展示「复杂的 ASIC 设计」全貌。

> 关于这张图的内容：它整体呈现的是 ASIC 设计的复杂流程/体系结构概览(呼应 README 上下文的「RTL→layout、RTL→GDSII」)。由于它是位图且分辨率/编码原因，本讲无法逐字解析图上每一个小字标签——**请你在本地直接打开这张 jpeg 阅读细节**，不要凭想象脑补图中的文字。

RTL 样例方面，最小、最适合入门的是 `MY-Design/`：

[MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v) —— 一个小型的层次化 Verilog 设计，U2 会逐行精读它。

而当你想见识「真实 SoC 有多复杂」时，去看 ARM 的 Cortex-M0 DesignStart(CMSDK)：

[cmsdk/cmsdk_mcu_system_zed.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v) —— 真实 MCU 的系统顶层，端口和信号远比 `MY_DESIGN` 多，U2-l2 会拿它做对比。

#### 4.3.4 代码实践

**实践目标**：打开配图、并用「文件大小」直观感受 RTL 复杂度的跨度。

**操作步骤**：

1. 用图片查看器打开 `Figures/Fig. Complex ASIC Design.jpeg`，花两分钟看懂它画了哪几个大块(例如前端/后端/签核等区域，以你实际看到的为准)。
2. 在终端比较两个 Verilog 的行数：`wc -l MY-Design/MY_DESIGN.v cmsdk/cmsdk_mcu_system_zed.v`。
3. 浏览 `MY-Design/MY_DESIGN.v` 的前 30 行，找到 `module` 关键字和端口列表。

**需要观察的现象**：`MY_DESIGN.v` 行数很少、端口寥寥几个；`cmsdk_mcu_system_zed.v` 行数和端口数量都大得多。

**预期结果**：你直观体会到「教学样例 → 真实 SoC」之间的复杂度落差，理解为什么后续讲义要从 `MY_DESIGN` 这个小例子讲起。

#### 4.3.5 小练习与答案

- **练习 1**：README 里那张图的链接指向的仓库名，和本仓库的名字一样吗？
  - **答案**：不一样。链接里是 `ASIC-Physical-Design-Roadmap`，本仓库是 `ASIC-Design-Roadmap`。图片的本地副本在 `Figures/` 下，看图应以本地文件为准。
- **练习 2**：仓库里有几个能被你直接读懂的 Verilog 设计目录？分别面向什么读者？
  - **答案**：三个——`MY-Design/`(极简，面向初学者)、`memories/`(单个存储模型)、`cmsdk/`(真实 ARM SoC，面向想见识复杂度的读者)。

---

### 4.4 CI 配置：它现在其实「什么都没做」

#### 4.4.1 概念说明

**CI(Continuous Integration，持续集成)** 是指每次 `push` 或发 `Pull Request` 时，服务器自动跑一段脚本(比如编译、测试)。GitHub 用 **GitHub Actions** 来做这件事，配置文件就放在仓库的 `.github/workflows/` 目录下。

很多开源项目都有「真正在跑测试」的 CI；但本仓库目前的 CI **只是 GitHub 自动生成的起步模板**，里面只有 `echo Hello, world!`。认清这一点，你就不会误以为「CI 通过 = 代码被验证过」。

#### 4.4.2 核心流程

```
开发者 push / 发 PR 到 main 分支
        │
        ▼
GitHub 读取 .github/workflows/blank.yml
        │
        ▼
在 ubuntu-latest 虚拟机上执行 steps：
   1) actions/checkout@v3   (把代码拷到虚拟机)
   2) echo Hello, world!     (仅打印一行)
   3) echo Add other actions ... (再打印两行)
        │
        ▼
任务结束(没有任何编译、没有任何测试)
```

也就是说：**这个 CI 的「绿灯」只代表 YAML 能被解析、虚拟机能启动，不代表代码正确**。

#### 4.4.3 源码精读

整个 CI 配置只有约 36 行，核心几处如下：

[.github/workflows/blank.yml:L3](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/.github/workflows/blank.yml#L3) —— `name: CI`，给这个工作流起名叫 `CI`。

[.github/workflows/blank.yml:L8-L11](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/.github/workflows/blank.yml#L8-L11) —— `on:` 触发条件：在向 `main` 分支 push 或发 PR 时触发，也支持手动 `workflow_dispatch`。

[.github/workflows/blank.yml:L30](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/.github/workflows/blank.yml#L30) —— `run: echo Hello, world!`，这是整个工作流「干活」的一行——仅仅打印一句话。文件开头还留着 GitHub 模板原话 `# This is a basic workflow to help you get started with Actions`，证实它就是未改动的起步模板。

> 结论：对本仓库而言，CI 目前只是占位。真正的「正确性验证」要靠你自己用 EDA 工具跑脚本、看报告——这正是 U4 之后讲义里大量 `report_*` 命令存在的意义。

#### 4.4.4 代码实践

**实践目标**：确认这份 CI 的真实内容，破除「CI 绿灯 = 已验证」的错觉。

**操作步骤**：

1. 打开 [.github/workflows/blank.yml](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/.github/workflows/blank.yml) 完整读一遍(很短)。
2. 数一下 `steps:` 下面有几个步骤、每个步骤实际做了什么。
3. 思考：如果想让 CI 真正检查 Verilog 语法，应该在哪里加步骤？(本讲只要求想，不必改——我们不准修改源码。)

**需要观察的现象**：除了 `actions/checkout@v3`(拉代码)之外，剩下的步骤全是 `echo`，没有任何 `iverilog`、`yosys`、`tclsh` 之类的调用。

**预期结果**：你得出结论「这份 CI 是 GitHub 起步模板，未做任何实质构建/测试」，并理解为什么学习 ASIC 时不能依赖 CI，而要看 EDA 报告。

#### 4.4.5 小练习与答案

- **练习 1**：这个工作流在什么情况下会被触发？
  - **答案**：向 `main` 分支 push、或针对 `main` 发 Pull Request 时自动触发；也支持在 Actions 页面手动触发(`workflow_dispatch`)。
- **练习 2**：看到仓库 Actions 页面是绿色对勾，能说明脚本逻辑正确吗？
  - **答案**：不能。当前 CI 只跑 `echo Hello, world!`，绿灯只说明 YAML 能解析、虚拟机能启动。脚本是否正确，要靠后续用真实 EDA 工具运行并检查报告来验证。

---

## 5. 综合实践

**任务**：制作一张属于你自己的「仓库目录树注释表」。

这是本讲规格里要求的核心实践，目的是把上面四个模块串起来，让你拿到任何 ASIC 类仓库都能快速建立导航。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files` 与 `ls -la`，把顶层目录和文件抄下来。
2. 用树状缩进画一张目录树(可参考 4.1.3)。
3. 为每个关键目录/文件写**一句话**作用说明，并把每个目录**对应到 u1-l2 设计流程的某一环**(如「库准备」「PnR 主流程」「STA 签核」等)。
4. 用 `★` 标出你认为「最重要、后续会反复用到」的 3 个路径(参考答案：`README.md`、`IC Compiler II/PnR.tcl`、`MY-Design/MY_DESIGN.v`)。
5. 额外挑战：在表里加一栏「备注」，记录你发现的**异常点**(例如根目录重复的 `.pl`、README 失效的 `Tutorials and Courses` 链接、图片链接指向的另一个仓库名)。

**需要观察的现象 / 预期结果**：

完成后，你应该拥有一张类似下表的可查索引：

| 目录/文件 | 一句话作用 | 对应流程环节 |
|-----------|------------|--------------|
| `README.md` ★ | 路线图总目录 | 总览 |
| `IC Compiler II/PnR.tcl` ★ | ICC2 物理设计主流程脚本 | PnR 主流程 |
| `MY-Design/MY_DESIGN.v` ★ | 最小可读 RTL 样例 | 前端 RTL |
| `PrimeTime/` | 静态时序分析脚本 | STA 签核 |
| `low_power.upf` | 多电压电源意图 | 低功耗 |
| … | … | … |

> 这一练习不依赖任何 EDA 工具，纯文件阅读即可完成；它也是后续每一讲「找文件」的前置准备。

---

## 6. 本讲小结

- 仓库**按工具/领域分目录**：`IC Compiler II/`、`IC Compiler/`、`PrimeTime/`、`mentor_scripts/` 是 EDA 脚本，`MY-Design/`、`cmsdk/`、`memories/` 是 RTL 样例。
- `README.md` 用**目录式导航**(Introduction / Fundamentals / ASIC Design Flow / Awesome Digital IC / Project Repos & IPs)组织文字内容。
- 参考资料**分门别类**放 PDF：可综合 HDL 规范、HDL Compiler 手册、yosys 手册、在线资源清单各有归宿。
- 仓库唯一的配图在 `Figures/Fig. Complex ASIC Design.jpeg`，但 README 里它的链接指向了**另一个名字相近的仓库**，看图以本地文件为准。
- `.github/workflows/blank.yml` 是 GitHub **起步模板**，只 `echo Hello, world!`，**没有真正构建或测试**，不要被绿灯误导。
- 有几处**链接/重复异常**需要自己核对：根目录与 `LEF2FRAM/` 下重复的 `.pl`、README 失效的 `Tutorials and Courses` 链接。

---

## 7. 下一步学习建议

现在你已经能在仓库里「找得到北」了，下一步建议：

1. **进入 U2(前端基础)**：从 4.3 提到的 `MY-Design/MY_DESIGN.v` 开始，正式逐行读懂第一个 Verilog 设计——讲义 u2-l1。
2. **同时认识时序约束**：打开 `MY-Design/My_Design.cons`，为 u2-l3 的 SDC 学习做准备。
3. **(可选)提前摸一眼主流程**：跳到 `IC Compiler II/PnR.tcl`，用 4.1.2 的对照表找出 setup/floorplan/.../finishing 各阶段注释，给 U4 预热(这部分 u1-l2 已带你看过大纲，这里只是再熟悉文件位置)。

> 记住本讲的核心方法论：**想看流程 → 进工具目录；想看 RTL → 进设计目录；想查语法 → 进 PDF 目录；遇到链接先 `git ls-files` 核对**。带着这张地图，后面任何一篇讲义你都知道它的素材从哪来。
