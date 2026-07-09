# 文档生成：build_docs 与 Sphinx

## 1. 本讲目标

本讲是专家层「工具、文档与贡献」单元的第一篇。前面几讲我们读的都是 VHDL/Python **源码**，本讲换个视角：hdl-modules 项目是怎么把「散落在仓库各处的 readme、模块源码头注释、寄存器 toml 清单、发布历史」**自动汇聚成一个可浏览的文档网站**（<https://hdl-modules.com>）的。

学完本讲，你应当能够：

1. 读懂 [tools/build_docs.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py) 的整条生成管线，说清楚它分哪几步、每一步产出什么。
2. 指出脚本里负责**生成寄存器 VHDL 包**和 **C++ 头文件**的具体 generator 调用，并理解这 7 个 generator 各自的职责。
3. 理解 `generate_documentation()` 如何借助 tsfpga 的 `ModuleDocumentation` **从源码头注释自动提取**模块文档。
4. 掌握 Sphinx 文档的目录布局、`conf.py` 关键配置（扩展、主题、intersphinx 交叉引用）以及 `getting_started.rst` 的章节组织。
5. 说清楚「文档侧的代码生成」与「仿真/构建流程里的代码生成」之间是什么关系——这是本讲实践任务的核心。

## 2. 前置知识

本讲建立在 [u1-l4（Python 入口与 tsfpga Module 模式）](u1-l4-python-entry-and-module-pattern.md) 之上。请先确认你理解以下概念：

- **tsfpga**：hdl-modules 依赖的上游 Python 库，提供 `get_modules`、`BaseModule`、`ModuleDocumentation`、`build_sphinx` 等基础设施。本项目的 `module_*.py`、`get_hdl_modules()` 都架在它之上。
- **hdl-registers**：另一个上游库，把寄存器 toml 清单「投影」成 VHDL/C 代码。我们在 [u7-l3（DMA 寄存器定义与 C++ 驱动）](u7-l3-dma-registers-and-cpp-driver.md) 已经见过它的产物。
- **VUnit**：仿真框架。

本讲用到几个文档领域的术语，先统一解释：

- **Sphinx**：Python 生态最常用的文档生成器。它读 **reStructuredText（RST）** 源文件，输出 HTML 网站（也能输出 PDF）。Sphinx 用 `conf.py` 做配置，用 `index.rst` 里的 `toctree`（目录树）组织页面层级。
- **reStructuredText / RST**：一种纯文本标记语言，类似 Markdown 但功能更强（原生支持交叉引用、指令 `.. xxx::`、片段包含等）。本仓库的 readme、各模块文档、本讲引用的代码块都是 RST。
- **generator（生成器）模式**：hdl-registers 把每种「要生成的文件」抽象成一个类（如 `VhdlRegisterPackageGenerator`），实例化时传入 `register_list`（寄存器清单）和 `output_folder`，再调 `.create_if_needed()` 落盘。这是本讲反复出现的代码套路。
- **toctree**：Sphinx 里声明「这一页下面挂着哪些子页面」的指令，相当于网站的导航树。

> 类比：如果说前面几讲是「电路怎么画」，本讲则是「**说明书怎么自动印刷**」。`build_docs.py` 就是那台印刷机：进料是仓库里的原始素材，出料是一个完整的网站。

## 3. 本讲源码地图

本讲只围绕文档生成这条链，涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `tools/build_docs.py` | **核心脚本**。编排整条文档生成管线：发布说明、引用条目、寄存器代码、模块文档、Sphinx 编译、徽章。 |
| `doc/sphinx/conf.py` | Sphinx 配置：启用的扩展、主题、交叉引用、站点地图、SEO/分析等。 |
| `doc/sphinx/getting_started.rst` | 手写的「快速上手」页面，讲依赖、源码集成方式、约束、寄存器接口。 |
| `tools/tools_env.py` | 路径单一信息源：定义 `doc/`、`generated/`、`modules/` 等目录的绝对路径。 |
| `hdl_modules/about.py` | README/slogan 的单一信息源，`build_docs.py` 由此取文案并**校验**根目录 `readme.rst` 一致。 |
| `.github/workflows/ci.yml` | CI 里真正执行 `python3 tools/build_docs.py` 并把 `generated/sphinx_html` 部署到 GitHub Pages 的地方。 |

## 4. 核心概念与源码讲解

### 4.1 build_docs.py 的整体管线

#### 4.1.1 概念说明

一个像 hdl-modules 这样有十几个模块、每个模块又有源码/测试/寄存器/约束的项目，文档如果全靠人手写，很快就会和源码脱节。`build_docs.py` 的设计哲学是：**把文档当成构建产物（build artifact），而不是手工维护的资产**。

它的角色类似于综合流程里的 `build_fpga.py`：本身不发明任何内容，只负责**编排**——调用一堆现成的「生成器」和「复制器」，把分散的素材汇聚到 `generated/` 目录下，最后交给 Sphinx 编译成 HTML。

关键点：脚本的产出目录是 **`generated/`（生成物）而不是 `doc/`（手写源）**。手写的 RST 放在 `doc/sphinx/`，机器生成的 RST 放在 `generated/sphinx_rst/`，两者在编译前合并，最终 HTML 落到 `generated/sphinx_html/`。这个「源 vs 生成物」的分离是理解整条管线的基础。

#### 4.1.2 核心流程

`main()` 是整条管线的总调度，顺序执行 8 个步骤：

1. **生成发布说明** `generate_and_create_release_notes()`：把 `doc/release_notes/*.rst` 汇编成一份 `release_notes.rst`。
2. **生成引用条目** `generate_bibtex()`：产出一个 BibTeX 片段页，方便学术论文引用本项目。
3. **生成寄存器代码制品** `generate_register_artifacts()`：对每个有寄存器的模块，跑 7 个 hdl-registers generator（详见 4.2）。
4. **生成模块文档** `generate_documentation()`：用 tsfpga 的 `ModuleDocumentation` 从源码提取每个模块的 RST，并拼出 `index.rst`。
5. **合并手写文档**：把 `doc/sphinx/` 下手写的文件/目录整体复制进 `generated/sphinx_rst/`。
6. **复制 banner**：把 logo 横幅拷到 HTML 输出目录。
7. **Sphinx 编译** `build_sphinx(...)`：把合并后的 RST 编译成 HTML 网站。
8. **生成徽章** `build_information_badges()`：画 license/github/website/chat 四个 SVG 徽章。

用伪代码表示就是：

```
main():
    生成 release_notes.rst          # → generated/sphinx_rst/
    生成 bibtex.rst                  # → generated/sphinx_rst/
    为每个有寄存器的模块生成 VHDL+C++  # → generated/sphinx_rst/modules/<名>/
    为每个模块提取 RST + 拼 index.rst  # → generated/sphinx_rst/modules/<名>/
    把 doc/sphinx/* 复制进 generated/sphinx_rst/   # 手写源与生成物合并
    build_sphinx(generated/sphinx_rst → generated/sphinx_html)
    画徽章 SVG                       # → generated/sphinx_html/badges/
```

#### 4.1.3 源码精读

先看路径常量，它们定义了「源在哪、生成物去哪」：脚本声明了三个根目录，把 `doc/sphinx/`（手写源）与 `generated/sphinx_rst/`（生成源）、`generated/sphinx_html/`（最终 HTML）严格分开。

[tools/build_docs.py:L45-L47](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L45-L47) —— 三个输出/源目录常量。

接着是 `main()` 本体，可以一眼看清 8 步的调用顺序：

[tools/build_docs.py:L53-L74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L53-L74) —— `main()` 管线，注意步骤 5 是用 `shutil.copyfile`/`copytree` 把手写文档合并进生成目录。

其中第 5 步「合并手写文档」值得单独看，它用一个循环把 `doc/sphinx/` 下**所有**文件和子目录（`css/`、`opengraph/`、各 `.rst`、`robots.txt`、logo 图等）原样复制到生成目录，这样 Sphinx 编译时手写源与生成源就在同一个根下：

[tools/build_docs.py:L63-L67](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L63-L67) —— 文件用 `copyfile`、目录用 `copytree(dirs_exist_ok=True)`，把整个手写文档树并入构建树。

最后，整条管线由 CI 在 `main` 分支上跑，产物 `generated/sphinx_html` 直接部署到 GitHub Pages：

[.github/workflows/ci.yml:L125-L144](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/.github/workflows/ci.yml#L125-L144) —— CI 先 `git fetch --all --tags`（发布说明依赖 git tag），再 `python3 tools/build_docs.py`，最后上传 `generated/sphinx_html` 为 Pages 产物。

> 注意 CI 第 127 行的注释：**没有 git tag，`build_docs.py` 就无法还原完整的发布历史**。这就是为什么 CI 必须 `fetch --all --tags`。一个看似无关的 Git 设置，会直接影响文档生成——这正是「文档即构建产物」的代价与体现。

#### 4.1.4 代码实践

**实践目标**：在不安装任何依赖的前提下，徒手追踪 `main()` 的 8 个步骤，画出「输入素材 → 输出文件」的对应表。

**操作步骤**：

1. 打开 [tools/build_docs.py:L53-L74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L53-L74)。
2. 对 `main()` 里的每一个调用（`generate_and_create_release_notes`、`generate_bibtex`、`generate_register_artifacts`、`generate_documentation`、复制循环、`build_sphinx`、`build_information_badges`），跳转到对应函数定义，确认它的**输出路径**写到哪个常量（`GENERATED_SPHINX` 还是 `GENERATED_SPHINX_HTML`）。
3. 列一张表：第几步 / 调用的函数 / 读什么 / 写到哪个目录。

**需要观察的现象**：你会发现前 4 步全部写进 `generated/sphinx_rst/`（RST 源），只有第 7、8 步写进 `generated/sphinx_html/`（HTML 产物）。徽章和 logo 是在 Sphinx 编译**之后**才补进 HTML 目录的——因为它们要直接出现在最终网站里，不需要经过 RST→HTML 转换。

**预期结果**：得到一张清晰的「管线分两段」的表——前半段产 RST，后半段产 HTML。

**运行验证**：本实践为源码阅读型，无需运行命令。若你本地已装齐 Sphinx/tsfpga/hdl-registers 等依赖，可执行 `python3 tools/build_docs.py` 后用 `find generated -maxdepth 3` 观察实际产物目录，与你的表对照；否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果有人误把手写的 `getting_started.rst` 放进了 `generated/sphinx_rst/` 而不是 `doc/sphinx/`，会发生什么？

**参考答案**：下次运行 `build_docs.py` 时，`generated/` 通常会被清理重建，该文件会被覆盖或删除，改动丢失。这就是为什么手写源必须放在 `doc/sphinx/`——它是受版本控制的「源」，`generated/` 是每次重建的「产物」。

**练习 2**：为什么 `build_information_badges()`（生成 SVG 徽章）放在 `build_sphinx()` **之后**，而不是之前？

**参考答案**：徽章是最终的 SVG/PNG 静态资源，要直接出现在 HTML 网站里被 `<img>` 引用，不经过 RST 编译。把它放在 Sphinx 编译之后写入 `generated/sphinx_html/badges/`，可以避免被 Sphinx 当成需要处理的 RST 源，逻辑上也更清晰（先建站，再挂装饰）。

---

### 4.2 寄存器制品生成：hdl-registers 的七个 generator

#### 4.2.1 概念说明

这是本讲最核心、也是实践任务直接指向的部分。

回顾 [u7-l3](u7-l3-dma-registers-and-cpp-driver.md)：一个有寄存器接口的模块（如 `dma_axi_write_simple`）会在自己目录下放一份 `regs_<名>.toml`，描述「有哪些寄存器、每个寄存器哪些位字段、各自是什么访问模式」。这份 toml 是**寄存器接口的单一信息源**。

hdl-registers 的工作，就是把这份 toml **投影**成多种语言的代码制品。在 hdl-modules 里一共用到 **7 个 generator**，分两组：

- **VHDL 侧 4 个**：寄存器包、record 包、AXI-Lite slave 壳、仿真读写包。
- **C++ 侧 3 个**：接口头（纯虚类）、具体类头、实现文件。

每个 generator 都遵循同一个套路：`Generator(register_list=..., output_folder=...).create_if_needed()`——传入寄存器清单和输出目录，按需生成文件。

#### 4.2.2 核心流程

`generate_register_artifacts()` 的逻辑很简单：

```
对 get_hdl_modules() 返回的每个模块 module：
    register_list = module.registers
    若 register_list 为 None（该模块没有 toml）→ 跳过
    否则：
        output_folder = generated/sphinx_rst/modules/<模块名>
        跑 4 个 VHDL generator  → 写入 output_folder/vhdl/
        跑 3 个 C++   generator  → 写入 output_folder/cpp/ 与 output_folder/cpp/include/
```

7 个 generator 的职责对照如下：

| generator | 语言 | 产物 | 作用 |
|-----------|------|------|------|
| `VhdlRegisterPackageGenerator` | VHDL | 寄存器包 `*_regs_pkg.vhd` | 寄存器/字段的地址常量、位宽、`register_mode_t` 定义 |
| `VhdlRecordPackageGenerator` | VHDL | record 包 `*_regs_record_pkg.vhd` | 把寄存器阵列聚合成 VHDL `record` 类型 |
| `VhdlAxiLiteWrapperGenerator` | VHDL | AXI-Lite slave 壳 | 可直接实例化的 AXI-Lite 寄存器文件实体 |
| `VhdlSimulationReadWritePackageGenerator` | VHDL | 仿真读写包 | testbench 里读写寄存器的辅助过程 |
| `CppInterfaceGenerator` | C++ | 接口头 `*.h` | 纯虚抽象类，定义寄存器访问接口 |
| `CppHeaderGenerator` | C++ | 具体类头 `*.h` | 实现该接口的类声明 |
| `CppImplementationGenerator` | C++ | 实现 `*.cpp` | 寄存器读写的具体实现 |

> 其中**生成「寄存器 VHDL 包」的是 `VhdlRegisterPackageGenerator`，生成「C++ 头文件」的是 `CppInterfaceGenerator` 与 `CppHeaderGenerator`**——这正是实践任务要你定位的两类调用。

#### 4.2.3 源码精读

脚本顶部一次性导入全部 7 个 generator，可以一次看清它们的命名空间归属（都在 `hdl_registers.generator.{vhdl,cpp}` 下）：

[tools/build_docs.py:L22-L30](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L22-L30) —— 7 个 generator 的导入，注意 VHDL 的 4 个在 `generator.vhdl.*`，C++ 的 3 个在 `generator.cpp.*`。

`generate_register_artifacts()` 用 `module.registers` 判断该模块有没有寄存器。**没有 toml 的模块（绝大多数）`registers` 为 `None`，直接 `continue` 跳过**，因此这一步只对 `dma_axi_write_simple`、`register_file` 等少数模块产生输出：

[tools/build_docs.py:L132-L137](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L132-L137) —— `register_list is None` 即跳过；输出目录按寄存器清单名（即模块名）分文件夹。

VHDL 侧 4 个 generator 依次实例化并 `create_if_needed()`，全部输出到 `.../vhdl/`：

[tools/build_docs.py:L139-L153](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L139-L153) —— 寄存器包、record 包、AXI-Lite 壳、仿真读写包四个 VHDL generator。

C++ 侧 3 个 generator，接口/头输出到 `.../cpp/include/`，实现输出到 `.../cpp/`：

[tools/build_docs.py:L155-L165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L155-L165) —— C++ 接口头、具体类头、实现文件三个 generator。

**与仿真/构建流程的关系（实践任务的关键）**：`build_docs.py` 在这里跑这 7 个 generator，**目的只是把生成出来的源码作为「文档」展示在网站上**（让读者点开网页就能看到生成的 VHDL/C++ 长什么样）。而在真正的仿真与综合流程里，这套生成是**由 tsfpga 的 `BaseModule` 自动完成的**——当一个模块带有 `regs_*.toml` 时，tsfpga 会自动生成寄存器 VHDL 包并加入文件列表。getting_started 文档把这一点讲得很明确：

[doc/sphinx/getting_started.rst:L105-L121](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L105-L121) —— 用 tsfpga 时「寄存器 HDL 代码在仿真和构建流程里自动生成并保持最新」；不用 tsfpga 时需手动集成 hdl-registers 的 VHDL/C/C++ 生成。

换句话说：**同一套 generator，在两条流程里各跑一次**——文档流程（`build_docs.py`）跑它是为了「展示」，仿真/构建流程（tsfpga）跑它是为了「真正编译进工程」。两者的代码生成逻辑完全一致，保证了网站上看到的代码和实际综合进 FPGA 的代码是同一份。

#### 4.2.4 代码实践

**实践目标**：定位「生成寄存器 VHDL 包」与「生成 C++ 头文件」的 generator 调用，并解释它们如何被集成进仿真与构建流程。（这正是本讲的总实践任务。）

**操作步骤**：

1. 在 [tools/build_docs.py:L127-L165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L127-L165) 找到 `generate_register_artifacts()`。
2. 指出生成 **VHDL 寄存器包** 的那一行（`VhdlRegisterPackageGenerator(...).create_if_needed()`，第 139–141 行）。
3. 指出生成 **C++ 头文件** 的两行（`CppInterfaceGenerator` 第 155–157 行、`CppHeaderGenerator` 第 159–161 行）。
4. 打开 [modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml)，确认这就是喂给这些 generator 的输入清单。
5. 阅读上面引用的 [getting_started.rst Register interfaces 段](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L105-L121)，回答「集成方式」问题。

**需要观察的现象 / 需要回答的问题**：

- 为什么 `build_docs.py` 里对 `module.registers is None` 的模块直接跳过？（答：只有带 toml 的模块才有寄存器，才需要生成。）
- 在仿真流程 `tools/simulate.py` 和构建流程 `tools/build_fpga.py` 里，**找不到**这 7 个 generator 的显式调用——为什么寄存器 VHDL 却能正常编译？（答：生成由 tsfpga `BaseModule` 在收集文件时隐式完成，见 getting_started 的说明。）

**预期结果**：你能用一句话说清——「`build_docs.py` 显式调用 hdl-registers 的 7 个 generator 是为了在文档网站展示生成代码；而实际工程里同一套生成由 tsfpga 在仿真/构建时自动完成，二者输入（toml）与输出一致。」

**运行验证**：若本地已装齐依赖，运行 `python3 tools/build_docs.py` 后查看 `generated/sphinx_rst/modules/dma_axi_write_simple/vhdl/` 与 `.../cpp/` 下生成的文件；否则标注「待本地验证」，仅完成源码阅读部分即可。

#### 4.2.5 小练习与答案

**练习 1**：`VhdlAxiLiteWrapperGenerator` 生成的「AXI-Lite slave 壳」和我们在 [u6-l1](u6-l1-register-file-core.md) 学的 `axi_lite_register_file` 是什么关系？

**参考答案**：生成的壳是一个**已经接好寄存器清单**的可实例化实体——它在内部实例化（或等价于）`axi_lite_register_file`，并把 toml 里声明的寄存器阵列以 generic 形式填好。用户无需手写 `registers`/`default_values` 两个 generic，直接实例化壳即可得到一个完整 AXI-Lite slave。

**练习 2**：C++ 侧为什么要把「接口头」和「具体类头」拆成两个 generator？

**参考答案**：接口头（`CppInterfaceGenerator`）产出**纯虚抽象类**，只定义「有哪些寄存器、怎么读写」的接口契约，不含实现；具体类头（`CppHeaderGenerator`）+ 实现（`CppImplementationGenerator`）产出可实例化的类。这样软件侧可以面向接口编程（如 [u7-l3](u7-l3-dma-registers-and-cpp-driver.md) 里手写的 `DmaNoCopy` 驱动架在生成接口之上），便于替换实现或做 mock 测试。

**练习 3**：如果一个模块新增了一份 `regs_*.toml`，文档网站的哪一页会自动多出 VHDL/C++ 源码？

**参考答案**：`generated/sphinx_rst/modules/<该模块名>/vhdl/` 与 `/cpp/` 目录会被这 7 个 generator 填充，进而出现在该模块的文档页里。前提是该模块出现在 `index.rst` 的模块 toctree 中（4.3 节会讲它如何自动列出所有模块）。

---

### 4.3 模块文档与 index.rst：tsfpga 的 ModuleDocumentation

#### 4.3.1 概念说明

4.2 节解决的是「寄存器代码怎么生成」，本节解决学习目标里的另一条：**文档与源码头注释自动提取的关系**。

hdl-modules 不为每个模块手写一份独立的说明文档——那样维护成本太高。它的做法是：让 tsfpga 的 `ModuleDocumentation` **扫描模块目录下的源码文件，从头注释（file header / entity 注释）里提取说明文字，自动拼成 RST 文档**。这样只要你在 VHDL 文件顶部写好注释，文档就会自动跟上。

`index.rst` 则是整本文档的**目录页**，用一个 `toctree` 把「关于 / 用户指南 / 各模块」三大块串成导航树，其中「模块」一栏的条目是**脚本遍历所有模块自动列出的**——新增一个模块目录，它的文档页就会自动出现在导航里。

#### 4.3.2 核心流程

`generate_documentation()` 的逻辑分两段：

```
第一段：拼 index.rst 的固定骨架
    写入手写的 toctree：
        About:        license_information / contributing / release_notes
        User guide:   getting_started / unresolved_types
        Modules:      （留空，下面动态填充）

第二段：遍历模块，动态填充 Modules 栏 + 逐个生成模块页
    modules = get_modules(modules_folder=modules/)
    modules_sorted = 按名字排序
    对每个 module：
        index_rst += "  modules/<名>/<名>\n"          # 在 toctree 增一条
        ModuleDocumentation(module, repository_url=..., repository_name="GitHub")
            .create_rst_document(output_path=..., exclude_module_folders=["rtl"])
        复制模块 doc/ 下的额外文件（图片等）
        复制 tsfpga 的符号图（用于示意）
    落盘 index.rst
```

两个细节值得注意：

- `repository_url` 用 `module.path.relative_to(REPO_ROOT)` 拼出指向 **GitHub 对应目录**的链接，让文档页上的「在 GitHub 上查看」按钮直链到源码。
- `exclude_module_folders=["rtl"]` 把每个模块下的 `rtl/`（netlist 构建 wrapper，见 [u8-l3](u8-l3-resource-utilization-regression.md)）排除出文档——这些是测试夹具，不该出现在用户文档里。

#### 4.3.3 源码精读

`index_rst` 的固定骨架定义了三大导航分组；`Modules` 一栏特意留空，等循环里动态追加：

[tools/build_docs.py:L168-L193](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L168-L193) —— `index.rst` 骨架，三个 `toctree` 分别带 `:caption: About / User guide / Modules`。

模块遍历的核心是这一段：按名排序、把模块加进 toctree、调用 `ModuleDocumentation.create_rst_document` 从源码提取 RST：

[tools/build_docs.py:L195-L215](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L195-L215) —— 注意 `exclude_module_folders=["rtl"]` 排除 netlist wrapper，`repository_url` 指向 GitHub 上该模块目录。

紧随其后还有两段文件复制：把模块 `doc/` 文件夹下**除了主 `<名>.rst` 之外的文件**（通常是插图）复制到输出目录，再把 tsfpga 自带的符号示意图（`TSFPGA_DOC/symbols/*.png`）也拷过去，供 RST 里 `.. image::` 引用：

[tools/build_docs.py:L217-L228](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L217-L228) —— 复制模块 doc 附带文件与公共符号图。

> 这就是「文档与源码头注释自动提取」的具体落地：`ModuleDocumentation` 读模块 `src/` 下每个 VHDL 文件的头部注释和实体声明，转成 RST 段落；文件列表、寄存器表（若有 toml）也一并提取。**你改了源码注释，下次构建文档就自动更新**——文档永不脱节。

#### 4.3.4 代码实践

**实践目标**：验证「新增/重命名一个模块目录，文档导航会自动跟上」。

**操作步骤**（源码阅读型，无需真的改源码）：

1. 读 [tools/build_docs.py:L195-L205](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L195-L205)，确认 `modules_sorted` 是按 `module.name` 字典序排序的。
2. 在 `modules/` 目录下列出全部模块名（用 `ls modules/`），按字典序排好。
3. 预测：网站导航的「Modules」栏里，第一个和最后一个模块分别是谁？

**需要观察的现象**：排序后，`axi` 应排在最前，`sine_generator` 排在最后（按字母序）。

**预期结果**：你预测的顺序与 <https://hdl-modules.com> 实际导航一致（可打开网站对照，或标注「待本地验证」）。

**延伸思考**：如果你想给某个模块的文档页加一段说明，应该编辑哪个文件？（答：编辑该模块 `src/` 下实体的头注释，或该模块 `doc/<名>.rst`——而不是去改 `generated/`。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ModuleDocumentation` 要 `exclude_module_folders=["rtl"]`？

**参考答案**：`rtl/` 下放的是 netlist 构建用的顶层 wrapper（见 [u8-l3](u8-l3-resource-utilization-regression.md) 的 `fifo_netlist_build_wrapper`），它们是测试/回归夹具，对最终用户没有意义，放进文档只会造成噪音。

**练习 2**：`index.rst` 的 `Modules` toctree 是手写的还是生成的？依据是什么？

**参考答案**：是**生成**的。骨架里这一栏为空，循环里对每个模块执行 `index_rst += f"  modules/{module.name}/{module.name}\n"` 动态追加。依据是 [tools/build_docs.py:L203-L204](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L203-L204)。

---

### 4.4 Sphinx 配置：conf.py 与文档目录结构

#### 4.4.1 概念说明

前面三节都在讲「怎么生成 RST 源」，本节讲「Sphinx 怎么把这些源编译成网站」。Sphinx 的行为由 `conf.py` 控制——它就是一个普通 Python 文件，里面给一堆全局变量赋值，Sphinx 读这些变量来决定启用哪些扩展、用什么主题、怎么交叉引用。

hdl-modules 的 `conf.py` 本身不复杂，但它体现了几个值得学习的工程做法：用 **intersphinx** 跨项目链接到 tsfpga/hdl-registers/VUnit 的官方文档；用 **sitemap/opengraph/googleanalytics** 扩展做 SEO 与社交分享；以及一套和 `build_docs.py` 相同的「优先本地仓库检出」的 PYTHONPATH 引导套路。

#### 4.4.2 核心流程

Sphinx 编译的输入输出：

```
输入：generated/sphinx_rst/（手写 doc/sphinx + 生成内容合并后的 RST 树，根为 index.rst）
配置：doc/sphinx/conf.py
输出：generated/sphinx_html/（HTML 网站）
```

`conf.py` 的关键配置项分四类：

1. **项目元信息**：`project`、`author`、`copyright`。
2. **扩展 `extensions`**：决定 Sphinx 的能力。
3. **主题与外观**：`html_theme`、`html_logo`、`html_static_path`。
4. **SEO/站点**：`intersphinx_mapping`（跨站交叉引用）、`html_baseurl`/`sitemap_url_scheme`（站点地图）、`googleanalytics_id`、`ogp_*`（社交预览）。

文档目录结构则由两部分合起来决定：手写的 `doc/sphinx/`（`conf.py`、`getting_started.rst`、`contributing.rst`、`license_information.rst`、`unresolved_types.rst`、`robots.txt`、`css/`、`opengraph/`、logo 图）+ 脚本生成的 `generated/sphinx_rst/`（`index.rst`、`release_notes.rst`、`bibtex.rst`、`modules/...`）。

#### 4.4.3 源码精读

`conf.py` 顶部和 `build_docs.py` 一样，做 REPO_ROOT 推导 + `sys.path.insert(0, ...)` + `import tools.tools_pythonpath`，确保优先用本地兄弟仓库的 tsfpga/hdl-registers（回顾 [u1-l3](u1-l3-toolchain-and-deps.md)）：

[doc/sphinx/conf.py:L17-L24](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L17-L24) —— 与工具脚本一致的 PYTHONPATH 引导。

启用的扩展列表定义了 Sphinx 的全部能力。注意里面**没有**任何「hdl-modules 自定义扩展」——全部是社区标准扩展，说明文档站不依赖私有 Sphinx 插件：

[doc/sphinx/conf.py:L30-L38](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L30-L38) —— `sphinx_rtd_theme`（主题）、`sphinx_sitemap`（站点地图）、`graphviz`/`symbolator_sphinx`（图/电路符号渲染）、`intersphinx`（跨站引用）、`googleanalytics`、`opengraph`。

`intersphinx_mapping` 是一个很实用的配置：它让本站文档里写 `:class:\`tsfpga.module.BaseModule\`` 这样的引用时，自动链接到 tsfpga 官网的对应页面。三个上游项目都映射了：

[doc/sphinx/conf.py:L40-L44](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L40-L44) —— 把 hdl-registers、tsfpga、vunit 三个官方文档站登记为交叉引用目标。

这也是为什么 `getting_started.rst` 里能直接写 `:py:meth:\`get_synthesis_files() <tsfpga.module.BaseModule.get_synthesis_files>\`` 而自动变成外链：

[doc/sphinx/getting_started.rst:L36-L49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L36-L49) —— 用 intersphinx 把 tsfpga 的 API 引用变成指向 tsfpga.com 的超链接。

主题与外观配置：

[doc/sphinx/conf.py:L59-L69](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L59-L69) —— 用 Read the Docs 主题、设 logo、声明 `css/` 与 `opengraph/` 为静态资源目录。

`getting_started.rst` 自身的章节结构则展示了「手写页面」的典型写法：用 `===`、`---`、`___` 三级标题，用 `.. _label:` 打交叉引用锚点，用 `.. warning::` 发警告框：

[doc/sphinx/getting_started.rst:L1-L31](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L1-L31) —— 顶部 `.. _getting_started:` 锚点 + Dependencies 小节，说明 VUnit 5.0.0+ 依赖与「排除 bfm 即零依赖」。

#### 4.4.4 代码实践

**实践目标**：弄清一个手写 RST 页面（`getting_started.rst`）是如何被「发现」并出现在网站导航里的。

**操作步骤**：

1. 读 [doc/sphinx/conf.py:L30-L44](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L30-L44)，列出启用的扩展与 intersphinx 目标。
2. 读 [tools/build_docs.py:L181-L186](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L181-L186)，注意 `User guide` toctree 里列了 `getting_started`。
3. 追问：`getting_started.rst` 这个文件本身在 `doc/sphinx/` 下，它是怎么进入 Sphinx 编译根 `generated/sphinx_rst/` 的？

**需要观察的现象**：你会回溯到 [tools/build_docs.py:L63-L67](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L63-L67) 的复制循环——整个 `doc/sphinx/` 被复制进 `generated/sphinx_rst/`，所以 `getting_started.rst` 才能被 `index.rst` 的 toctree 引用到。

**预期结果**：你能讲清「一个手写页面从落地到出现在导航」的完整链路：写进 `doc/sphinx/` → 被 `main()` 复制进 `generated/sphinx_rst/` → 被 `index.rst` 的 toctree 引用 → Sphinx 编译成 HTML 页 → 出现在网站侧边栏。

**运行验证**：源码阅读型实践，无需运行；若想验证 intersphinx 效果，可在本地构建文档后点击 `getting_started` 页里的 tsfpga API 链接，确认跳转到 tsfpga.com（标注「待本地验证」）。

#### 4.4.5 小练习与答案

**练习 1**：如果你想让文档站支持数学公式，需要改 `conf.py` 的哪个配置？

**参考答案**：在 `extensions` 列表里加入 `sphinx.ext.mathjax`（或 `imgmath`）。`conf.py` 的 `extensions` 就是 Sphinx 的「能力开关」。

**练习 2**：`html_static_path = ["css", "opengraph"]` 里的路径是相对什么解析的？

**参考答案**：相对 `conf.py` 所在目录（即 `doc/sphinx/`）。但因为 `build_docs.py` 把整个 `doc/sphinx/` 复制进了 `generated/sphinx_rst/`，Sphinx 实际运行时的根是 `generated/sphinx_rst/`，`conf.py` 与 `css/`、`opengraph/` 一并被复制过去，所以相对路径依然有效。

**练习 3**：为什么 `conf.py` 顶部要 `import tools.tools_pythonpath`？

**参考答案**：为了和工具脚本保持一致的依赖解析策略——优先使用本地检出的 tsfpga/hdl-registers 兄弟仓库，而不是 pip 装的版本，确保文档构建用的库版本与开发一致（见 [u1-l3](u1-l3-toolchain-and-deps.md) 的 `tools_pythonpath` 讲解）。

---

## 5. 综合实践

把本讲四节串起来，完成一次「**从一根 toml 到网站上一页文档**」的全程追踪。

**任务**：以 `dma_axi_write_simple` 模块为对象，复现它的文档页是如何被生产出来的。

**步骤**：

1. **输入侧**：打开 [modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml)，这是寄存器清单的单一信息源。回忆 [u7-l3](u7-l3-dma-registers-and-cpp-driver.md) 它定义了哪些寄存器。

2. **代码生成侧**：在 [tools/build_docs.py:L127-L165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L127-L165) 确认这个模块（因为 `module.registers` 非 `None`）会触发全部 7 个 generator，产物落到 `generated/sphinx_rst/modules/dma_axi_write_simple/vhdl/` 与 `/cpp/`。

3. **文档提取侧**：在 [tools/build_docs.py:L211-L215](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L211-L215) 确认 `ModuleDocumentation` 会扫描该模块 `src/` 的头注释，生成模块说明 RST，并把 GitHub 链接指向该模块目录。

4. **导航侧**：在 [tools/build_docs.py:L203-L204](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L203-L204) 确认 `dma_axi_write_simple` 会作为一行被追加进 `index.rst` 的 Modules toctree。

5. **编译侧**：在 [tools/build_docs.py:L72](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L72) 确认 `build_sphinx` 把上述全部 RST 编译成 `generated/sphinx_html/modules/dma_axi_write_simple/dma_axi_write_simple.html`。

**交付物**：画一张流程图（文字版即可），标注「toml → 7 个 generator → RST 文件 → ModuleDocumentation 补充说明 → index.rst 导航 → build_sphinx → HTML 页」，并在每个箭头旁写出对应的 `build_docs.py` 行号。

**进阶（可选）**：思考如果该模块**没有** toml（如 `fifo` 模块），上述链路的哪几步会消失？（答：第 2 步的 7 个 generator 整体跳过，因为 `module.registers is None`；其余步骤照常。）

**运行验证**：若本地已装齐依赖，运行 `python3 tools/build_docs.py` 后用浏览器打开 `generated/sphinx_html/index.html`，进入 dma_axi_write_simple 模块页，确认能看到生成的寄存器 VHDL/C++ 源码与 GitHub 链接；否则标注「待本地验证」。

## 6. 本讲小结

- `build_docs.py` 是文档版的「构建脚本」：它不发明内容，只编排——把发布说明、引用条目、寄存器代码、模块文档汇聚到 `generated/sphinx_rst/`，再由 `build_sphinx` 编译成 `generated/sphinx_html/`。**文档是构建产物，不是手工资产。**
- 手写源（`doc/sphinx/`）与生成物（`generated/sphinx_rst/`）严格分离，编译前由 `main()` 的复制循环合并；这与「源码 vs 综合产物」的分离同理。
- 寄存器代码由 hdl-registers 的 **7 个 generator** 生成（4 个 VHDL：寄存器包/record 包/AXI-Lite 壳/仿真读写包；3 个 C++：接口头/具体类头/实现）。其中 `VhdlRegisterPackageGenerator` 生 VHDL 包，`CppInterfaceGenerator`+`CppHeaderGenerator` 生 C++ 头。
- 这 7 个 generator 在文档流程里被 `build_docs.py` **显式**调用，目的是把生成代码展示在网站上；而在仿真/构建流程里，同一套生成由 **tsfpga `BaseModule` 自动**完成（依据 `getting_started.rst` 的 Register interfaces 段）。两路输入同一 toml、输出一致。
- 模块文档由 tsfpga 的 `ModuleDocumentation` **从源码头注释自动提取**，`index.rst` 的 Modules 导航由脚本遍历模块目录**动态生成**——改源码注释即更新文档，新增模块即出现在导航。
- `conf.py` 用社区标准扩展（rtd 主题、intersphinx、sitemap、opengraph、graphviz/symbolator）配置 Sphinx，并通过 intersphinx 把 tsfpga/hdl-registers/VUnit 的 API 引用自动变成跨站超链接。

## 7. 下一步学习建议

- 下一讲 [u9-l2 Netlist 综合与 FPGA 构建流程](u9-l2-netlist-build-fpga-flow.md) 会转向**电路侧**的构建脚本 `tools/synthesize.py` 与 `tools/build_fpga.py`，与本讲构成「文档构建」与「电路构建」的对照——两者都遵循「脚本编排 + tsfpga/hdl-registers 基础设施」的同构思路。
- 想深入了解寄存器代码生成背后的 hdl-registers 库，可去 <https://hdl-registers.com> 阅读它的 generator 文档（本讲的 intersphinx 已把它配为交叉引用目标）。
- 想看真实的生成产物长什么样，可对照 [u7-l3（DMA 寄存器定义与 C++ 驱动）](u7-l3-dma-registers-and-cpp-driver.md)，那里手写 VHDL/C++ 如何消费本讲生成的寄存器包与接口头有完整讲解。
- 若你对文档发布流程感兴趣，可接着读 [u9-l3 发布工程、Lint 与贡献规范](u9-l3-release-lint-contributing.md)，它讲 `tag_release.py` 如何管理版本与发布说明（而发布说明正是本讲 `generate_and_create_release_notes()` 的输入）。
