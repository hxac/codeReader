# 文档生成：build_docs 与 Sphinx

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说出 `tools/build_docs.py` 这一个脚本如何**从零构建出整个 hdl-modules 网站**——它经历了哪几个阶段，每个阶段产出什么。
2. 在源码里指认出调用 hdl-registers 生成寄存器 **VHDL 包**与 **C++ 头文件**的那几行 `Generator` 调用，并说清这些生成物在**文档**里和在**仿真/构建流程**里分别如何被使用。
3. 讲清 tsfpga 的 `ModuleDocumentation` 如何把每个模块的源码头注释、寄存器清单「自动提取」成 RST 文档，以及 `build_sphinx` 如何把它们编译成 HTML 网站。
4. 读懂 `doc/sphinx/conf.py` 的关键配置项（extensions、intersphinx、theme），以及 `getting_started.rst` 揭示的「tsfpga 流程 vs 手动流程」两条集成路线。
5. 自己能改一处模块文档或加一个寄存器，再跑一次文档生成看到它出现在网站上。

## 2. 前置知识

本讲是专家层（advanced），承接 **u1-l4**（Python 入口与 tsfpga Module 模式）。在动手前，请确认你已经理解：

- **`get_hdl_modules()`**：`hdl_modules/__init__.py` 提供的入口，扫描 `modules/` 目录返回一组 tsfpga `Module` 对象（[hdl_modules/__init__.py:28-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py#L28-L50)）。文档脚本反复调用它来「枚举所有模块」。
- **`module_*.py` + `BaseModule`**：每个模块目录下都有一个 `module_<名>.py`，其中定义继承 `BaseModule` 的 `Module` 类。本讲会用到它的两个属性：`module.registers`（寄存器清单，没有就为 `None`）和 `module.path`（模块在磁盘上的路径）。这两个属性是文档自动提取的「数据源」。
- **hdl-registers 与寄存器 toml**：一些模块（如 `dma_axi_write_simple`）在目录下放一个 `regs_*.toml`，描述寄存器与位字段。tsfpga 的 `BaseModule` 在加载模块时会解析这个 toml，得到 `module.registers`。**这是 u7-l3 的前置认知**，本讲不重复 toml 语法，只用其产物。

如果下面这些名词你不熟，先记一句话定义：

- **RST（reStructuredText）**：一种纯文本标记语言，是 Sphinx 的源格式，类似 Markdown 但功能更强（支持 `.. directive::` 指令、交叉引用 `:ref:`）。
- **Sphinx**：Python 文档生态的事实标准工具，把一组 `.rst` 文件编译成 HTML 网站（Python 官方文档就用它）。本项目的网站 [hdl-modules.com](https://hdl-modules.com) 就是 Sphinx 产物。
- **Generator（生成器）**：hdl-registers 提供的一类对象，吃一份寄存器清单、吐出一份特定语言的源码文件（VHDL 包、C++ 头等）。调用模式固定：`XxxGenerator(register_list=..., output_folder=...).create_if_needed()`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tools/build_docs.py` | 本讲主角。一个脚本完成「生成寄存器代码 → 生成模块 RST → 复制文件 → 跑 Sphinx → 生成徽章」全流程。 |
| `doc/sphinx/conf.py` | Sphinx 配置文件。声明用哪些扩展、什么主题、跨项目链接（intersphinx）指向哪里。 |
| `doc/sphinx/getting_started.rst` | 手写的用户指南 RST。揭示了「tsfpga 自动流程」与「手动流程」两条把模块接进工程的路线。 |
| `tools/tools_env.py` | 路径单一信息源。`HDL_MODULES_GENERATED`、`HDL_MODULES_DOC` 决定生成物写到哪。 |
| `hdl_modules/about.py` | README/slogan 的单一信息源，文档脚本据此拼出首页与 BibTeX。 |

> 说明：`ModuleDocumentation`、`build_sphinx`、`generate_release_notes` 以及各 `Generator` 类都来自外部依赖 **tsfpga / hdl-registers**（仓库外，需另装）。本讲依据项目中对它们的**真实调用方式**讲解语义；涉及库内部实现细节处会标注「待确认」。

## 4. 核心概念与源码讲解

### 4.1 全局视角：一个脚本如何构建整个网站

#### 4.1.1 概念说明

很多项目把「文档」当成与代码割裂的东西——README 手写一份、API 文档另用工具扫一遍、示例再单独维护。hdl-modules 的取向相反：**尽量让单一信息源自动生成一切**，`tools/build_docs.py` 就是这个哲学的集中体现。

它要做的事可以归成三大类：

1. **生成代码产物**：对每个带寄存器的模块，调用 hdl-registers 生成 VHDL/C++ 源码。这些产物在文档里以「可下载/可查看的源码」形式展示，让读者无需克隆仓库就能看到生成的寄存器包长什么样。
2. **生成文档内容**：对每个模块，调用 tsfpga 的 `ModuleDocumentation` 扫描源码头注释、寄存器清单，自动写成 RST；同时手写 release notes、BibTeX、README 校验。
3. **编译成网站**：把所有 RST 收拢进 `index.rst` 的 toctree（目录树），交给 Sphinx 编译成 HTML，最后用 pybadges 画几个 SVG 徽章（license、GitHub、网站、讨论区）。

关键设计是：**这些产物全部写到 `generated/` 目录，绝不污染源码树**（`generated/` 不入库，构建时按需重生）。这和 u8-l3 讲的「netlist 资源回归」写到 `generated/` 是同一个约定。

#### 4.1.2 核心流程

`main()` 是整条流水线的总指挥，五个阶段顺序执行，最后收尾：

```text
main()
  │
  ├─① generate_and_create_release_notes()
  │     扫描 doc/release_notes/，拼出 release_notes.rst
  │
  ├─② generate_bibtex()
  │     用 about.py 的 slogan 拼出引用条目 bibtex.rst
  │
  ├─③ generate_register_artifacts()
  │     遍历 get_hdl_modules()
  │     对每个有 registers 的模块 → 跑 4 个 VHDL + 3 个 C++ generator
  │     产物写到 generated/sphinx_rst/modules/<模块名>/{vhdl,cpp}/
  │
  ├─④ generate_documentation()
  │     遍历所有模块 → ModuleDocumentation().create_rst_document()
  │     自动提取源码头注释 + 寄存器表 → generated/sphinx_rst/modules/<模块名>/<模块名>.rst
  │     同时拼出 index.rst（带 toctree 目录树）
  │
  ├─  复制 doc/sphinx/ 下手写文件 → generated/sphinx_rst/
  ├─  复制 banner.png            → generated/sphinx_html/logos/
  │
  ├─⑤ build_sphinx(build_path=generated/sphinx_rst, output_path=generated/sphinx_html)
  │     Sphinx 编译 RST → HTML 网站
  │
  └─  build_information_badges()
        pybadges 画 license/github/website/chat 四个 SVG 徽章
```

三个关键路径常量来自 [tools/tools_env.py:15-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_env.py#L15-L16)（`HDL_MODULES_DOC = doc/`、`HDL_MODULES_GENERATED = generated/`），脚本把它们组合成工作目录：

- `GENERATED_SPHINX = generated/sphinx_rst/` —— Sphinx 的**源**目录（放 `.rst`）。
- `GENERATED_SPHINX_HTML = generated/sphinx_html/` —— Sphinx 的**输出**目录（放 `.html`）。
- `SPHINX_DOC = doc/sphinx/` —— 手写 RST 与配置所在的源目录。

#### 4.1.3 源码精读

`main()` 的结构非常扁平，就是五个函数调用加几段复制逻辑，见 [tools/build_docs.py:53-74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L53-L74)。这一段中文说明：先依次生成 release notes、BibTeX、寄存器产物、模块文档，然后把 `doc/sphinx/` 下的手写文件整体复制进 `generated/sphinx_rst/`（这一步保证 `conf.py`、`getting_started.rst` 等也进入构建目录），再复制 banner、跑 Sphinx、画徽章。

工作目录常量定义在 [tools/build_docs.py:45-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L45-L50)，这段把 `tools_env` 的路径与脚本自身的输出目录拼起来——注意所有写操作都落在 `generated/` 下，源码树保持干净。

值得专门看的是「复制手写文件」这段 [tools/build_docs.py:63-67](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L63-L67)：它遍历 `doc/sphinx/` 下所有条目，文件用 `copyfile`、目录用 `copytree(dirs_exist_ok=True)`。这把 `conf.py`、`getting_started.rst`、`contributing.rst`、`css/`、`opengraph/` 等全部搬进 `generated/sphinx_rst/`——所以 Sphinx 实际编译的是一个**手写 + 自动生成混合**的目录，`conf.py` 仍是那份手写的。

#### 4.1.4 代码实践

**实践目标**：建立对整条流水线「输入→产物」的整体印象。

**操作步骤**：

1. 打开 [tools/build_docs.py:53-74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L53-L74)，把 `main()` 里五个函数调用与上面的流程图一一对应。
2. 在仓库根目录执行 `python3 tools/build_docs.py`（需已按 u1-l3 装好 tsfpga / hdl-registers / Sphinx 等依赖；若无依赖环境，则改为纯阅读）。
3. 构建完成后用 `ls generated/sphinx_rst/` 和 `ls generated/sphinx_html/` 观察产物。

**需要观察的现象**：

- `generated/sphinx_rst/` 下应同时出现手写文件（`getting_started.rst`、`conf.py`）和自动生成的文件（`index.rst`、`release_notes.rst`、`modules/` 目录）。
- `generated/sphinx_html/` 下应出现 `.html` 网页和 `badges/*.svg`。

**预期结果**：HTML 网站能在浏览器打开（入口 `generated/sphinx_html/index.html`）。

**待本地验证**：若依赖未装全，本步骤无法真实执行，改为纯源码阅读型实践——只做步骤 1 的对应即可。

#### 4.1.5 小练习与答案

**练习 1**：`main()` 里如果删掉 `generate_register_artifacts()` 调用，网站会缺什么？
**答案**：会缺每个带寄存器模块的「生成代码」展示页（VHDL 包、C++ 头/实现等可查看源码），但模块的主文档页（`ModuleDocumentation` 产物）仍会生成，因为寄存器**表格**是由 `ModuleDocumentation` 另行提取的，与 `generate_register_artifacts` 生成的**代码文件**是两回事。

**练习 2**：为什么所有写操作都落在 `generated/` 而不是 `doc/`？
**答案**：`doc/` 是手写、入库的源；`generated/` 是构建产物、不入库。把生成物隔离到 `generated/` 能保证源码树干净、可重复构建、不产生无意义的 git diff（与 u8-l3 的 netlist 产物同理）。

---

### 4.2 寄存器代码生成：build_docs.py 里的 Generator 调用

#### 4.2.1 概念说明

这是本讲的核心。hdl-registers 的设计是「**一份寄存器清单，多份语言产物**」：同一份 `module.registers`（由 toml 解析而来），可以被不同的 `Generator` 投影成不同的源码文件。

在 `build_docs.py` 里，这些 generator 的产物是给**文档**用的——把生成的 VHDL/C++ 代码摆到网站上，让读者直观看到「toml 描述的寄存器，最终长成了什么样的代码」。这一点要和 u7-l3 讲的「仿真/构建流程里的代码生成」**区分清楚**：

- **文档流程**（本讲）：`build_docs.py` 显式调用 7 个 generator，产物落到 `generated/sphinx_rst/modules/<模块名>/`，**只用于展示**。
- **仿真/构建流程**（u1-l4、u7-l3）：当用户通过 tsfpga 调 `get_synthesis_files()` / `get_simulation_files()` 时，`BaseModule` **自动**运行寄存器生成器、把生成的 VHDL 加入文件列表——这条路径**不在 `build_docs.py` 里**，而是 tsfpga 内部完成的。getting_started.rst 里那句「register HDL code is automatically generated and kept up to date」指的就是这条路径。

换句话说：`build_docs.py` 里的 generator 调用是「为了让网站能展示生成代码」而**重复**了一遍生成逻辑；真正喂给综合/仿真的代码，是 tsfpga 在工程构建时即时生成的。两者用同一套 generator、同一份 toml，所以产物一致。

#### 4.2.2 核心流程

`generate_register_artifacts()` 的逻辑很规整：遍历模块 → 跳过没有寄存器的 → 对有寄存器的逐个跑 generator。

```text
for module in get_hdl_modules():
    register_list = module.registers
    if register_list is None:          # 这个模块没有 toml，跳过
        continue

    output_folder = generated/sphinx_rst/modules/<register_list.name>/

    # 4 个 VHDL generator → output_folder/vhdl/
    VhdlRegisterPackageGenerator     → regs_<name>.vhd       （寄存器地址/位定义包）
    VhdlRecordPackageGenerator       → <name>_regs_pkg.vhd   （record 类型包）
    VhdlAxiLiteWrapperGenerator      → <name>_reg_file.vhd   （AXI-Lite slave 壳）
    VhdlSimulationReadWritePackageGenerator → <name>_reg_operations.vhd （仿真读写包）

    # 3 个 C++ generator
    CppInterfaceGenerator → output_folder/cpp/include/  （接口抽象头）
    CppHeaderGenerator    → output_folder/cpp/include/  （实现头）
    CppImplementationGenerator → output_folder/cpp/      （实现 cpp）
```

七个 generator 分两类：

- **VHDL 侧 4 个**：寄存器包（地址/位宽常量）、record 包（把寄存器捆成 record 类型，u7-l3 提到的 `regs_up`/`regs_down` 就基于它）、AXI-Lite 包装壳（可直接实例化的 slave，u6-l1 的 `axi_lite_register_file` 同类）、仿真读写包（testbench 里 `read_reg`/`write_reg` 用的）。
- **C++ 侧 3 个**：接口头（纯虚基类）、实现头 + 实现 cpp（实际驱动，u7-l3 的 C++ 驱动就架在这之上）。

每个 generator 都用 `.create_if_needed()` 而非 `.create()`——这是 hdl-registers 的惯用法：仅在文件不存在或内容变化时才写盘，避免无谓的重建。

#### 4.2.3 源码精读

generator 的导入集中在文件顶部 [tools/build_docs.py:22-30](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L22-L30)，这段中文说明：从 `hdl_registers.generator.vhdl.*` 与 `hdl_registers.generator.cpp.*` 各自 import 需要的 generator 类。注意这些 import 出现在 `tools_pythonpath` 之后（[tools/build_docs.py:20](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L20)），延续了 u1-l3 讲过的「先 insert PYTHONPATH，再 import 第三方包」的引导套路。

遍历与跳过逻辑见 [tools/build_docs.py:132-137](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L132-L137)：用 `get_hdl_modules()`（u1-l4 的入口）枚举所有模块，`module.registers is None` 时 `continue`——所以像 `fifo`、`common` 这些没有 toml 的模块根本不会进这条路，只有 `dma_axi_write_simple` 等带寄存器的模块会。输出目录用 `register_list.name`（即模块名）命名。

4 个 VHDL generator 的调用见 [tools/build_docs.py:139-153](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L139-L153)，3 个 C++ generator 的调用见 [tools/build_docs.py:155-165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L155-L165)。每行都是同一个模式 `XxxGenerator(register_list=register_list, output_folder=...).create_if_needed()`，只是 output_folder 的子目录不同（VHDL 进 `vhdl/`，C++ 头进 `cpp/include/`、实现进 `cpp/`）——这与 u7-l3 讲的「4 个 VHDL 制品 + 3 个 C++ 制品」完全对应，只是这里把它们落到了文档目录。

#### 4.2.4 代码实践

**实践目标**：把「生成寄存器 VHDL 包与 C++ 头文件的 generator 调用」找出来，并说清它们与仿真/构建流程的关系。

**操作步骤**：

1. 在 [tools/build_docs.py:127-165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L127-L165) 里圈出三个 generator：生成 **VHDL 寄存器包**的是 `VhdlRegisterPackageGenerator`（L139-141）、生成 **C++ 头文件**的是 `CppHeaderGenerator`（L159-161）。
2. 打开 [doc/sphinx/getting_started.rst:105-121](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L105-L121)，阅读「Register interfaces」一节。
3. 对照思考：getting_started.rst 说「用 tsfpga 时寄存器 HDL 自动生成并保持最新」，而 `build_docs.py` 又**显式**调了一遍 generator——这两份产物分别用在哪？

**需要观察的现象 / 预期结果**（源码阅读型实践）：

- `build_docs.py` 的产物 → 落到 `generated/sphinx_rst/modules/<模块名>/vhdl/` 与 `.../cpp/`，**作为网站上的可查看代码展示**，不参与综合。
- tsfpga 流程的产物 → `BaseModule` 在你调 `get_synthesis_files()` 时即时生成、加入文件列表，**真正喂给 Vivado/VUnit**。getting_started.rst 的「Manual workflow」一节（L114-121）正是告诉不走 tsfpga 的用户：你必须**自己**把 hdl-registers 的 VHDL/C++ 生成集成进流程——因为没有了 tsfpga，就没有人替你自动跑 generator。

**结论**：`build_docs.py` 里的 generator 调用是「为文档而生成」，与仿真/构建流程里的「为综合而生成」是两条独立路径，但共用同一套 generator 与同一份 toml，所以代码内容一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate_register_artifacts` 要 `if register_list is None: continue`，而不是直接对每个模块跑 generator？
**答案**：只有带 `regs_*.toml` 的模块才有 `module.registers`（非 `None`）；`fifo`、`resync` 等模块没有 toml，`registers` 为 `None`，对它们跑 generator 既无意义也会报错。`continue` 提前剔除。

**练习 2**：如果一个模块的 toml 改了一个寄存器位宽，`build_docs.py` 重跑后，文档网站上哪两处会变？
**答案**：① `generate_register_artifacts` 重新生成的 VHDL/C++ 代码文件（展示用）；② `ModuleDocumentation` 提取出的寄存器**表格**（因为表格也读 `module.registers`）。两处都源自同一份 toml，故会同步更新。

---

### 4.3 模块文档与 Sphinx 网站：ModuleDocumentation + build_sphinx

#### 4.3.1 概念说明

上一节生成的是「代码文件」，本节生成的是「人读的文档」。tsfpga 提供的 `ModuleDocumentation` 是关键：给它一个 `Module` 对象，它会扫描该模块目录下的源码、头注释、寄存器清单，自动拼出一份 RST 文档——包含模块说明、源码实体列表、寄存器表、仿真模型说明等。这意味着**文档与源码绑定**：改了源码头注释，下次构建网站就自动反映出来，不需要单独维护文档。

拼好所有模块的 RST 后，还需要一个 `index.rst` 作为网站入口，用 Sphinx 的 `toctree`（目录树）指令把各页串起来。`index.rst` 里的每个 toctree 块对应网站侧边栏的一个分组（About / User guide / Modules）。

最后 `build_sphinx` 调用 Sphinx 引擎，把整个 `generated/sphinx_rst/` 目录编译成 `generated/sphinx_html/` 下的 HTML。

#### 4.3.2 核心流程

```text
generate_documentation()
  │
  ├─ 先拼 index.rst 的骨架：README 正文 + 三个 toctree（About/User guide/Modules）
  │
  ├─ modules = get_modules(modules_folder=modules/)   # 注意：直接用 tsfpga 的 get_modules
  ├─ modules 按 name 排序
  │
  ├─ for module in modules_sorted:
  │     ① 把模块名追加进 index.rst 的 Modules toctree
  │     ② ModuleDocumentation(module, repository_url=...).create_rst_document(
  │            output_path=generated/sphinx_rst/modules/<名>,
  │            exclude_module_folders=["rtl"])        # 排除 rtl/（netlist 夹具，不进文档）
  │     ③ 复制模块 doc/ 目录下的附带文件（图片等）
  │     ④ 复制 tsfpga 自带的符号图片（symbols/*.png）
  │
  └─ create_file(index.rst)
```

两个易被忽略的细节：

1. **`get_modules` 而非 `get_hdl_modules`**：这里直接调 tsfpga 的 `get_modules(modules_folder=...)`（[tools/build_docs.py:195](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L195)），没走 `get_hdl_modules()` 那层包装。区别在于 `get_hdl_modules()` 固化了 `library_name_has_lib_suffix=False`（库名约定，u1-l4），而文档生成只关心模块清单与路径，不涉及库名，所以直接用底层函数即可。
2. **排除 `rtl/`**：`exclude_module_folders=["rtl"]`（[tools/build_docs.py:215](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L215)）把每个模块里的 `rtl/` 目录挡在文档之外。回忆 u1-l2 与 u8-l3：`rtl/` 放的是 netlist 构建用的顶层 wrapper（如 `fifo_netlist_build_wrapper.vhd`），那是给综合回归用的夹具，不属于用户关心的接口，不该出现在模块文档里。

#### 4.3.3 源码精读

`index.rst` 的骨架与三个 toctree 见 [tools/build_docs.py:169-193](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L169-L193)，这段中文说明：首页先放 README 正文，再用三个 `.. toctree::` 指令分别列出 About（license、contributing、release_notes）、User guide（getting_started、unresolved_types）、Modules（留空，下面循环里逐个追加）三组导航。`:hidden:` 表示不在首页正文显示、只进侧边栏。

模块遍历与 `ModuleDocumentation` 调用见 [tools/build_docs.py:203-228](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L203-L228)。注意 `repository_url` 用了 `f"{REPOSITORY_URL}/tree/main/{module.path.relative_to(REPO_ROOT)}"`，即给每个模块的文档页生成一个指向该模块在 GitHub 上 `main` 分支对应目录的链接——这是文档里「源码永久链接」的来源（读者能从文档一键跳到 GitHub 源码）。后面两段 `for` 循环分别复制模块 `doc/` 下的附带文件（L222-224）和 tsfpga 自带的符号图片（L227-228）。

README 校验逻辑见 [tools/build_docs.py:233-249](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L233-L249)，这段中文说明：因为 GitHub 的 README 不支持 RST 文件包含指令，项目把 README 内容在两处维护（GitHub 版与网站版，见 [hdl_modules/about.py:25-63](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/about.py#L25-L63) 的注释）。`get_readme()` 在生成网站首页前，先用 `include_extra_for_github=True` 重算一份，与仓库根的 `readme.rst` 逐字节比对——不一致就直接 `raise ValueError`，把生成的参考文件写到 `readme.txt` 供对比。这是一道**一致性护栏**：防止两份 README 漂移。

Sphinx 编译调用见 [tools/build_docs.py:72](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L72)：`build_sphinx(build_path=GENERATED_SPHINX, output_path=GENERATED_SPHINX_HTML)`，把混合目录编译成 HTML。`build_sphinx` 与前面用的 `generate_release_notes` 都从 `tsfpga.tools.sphinx_doc` 导入（[tools/build_docs.py:36](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L36)），即 tsfpga 已替本项目封装好了 Sphinx 调用。

#### 4.3.4 代码实践

**实践目标**：体验「源码头注释 → 自动文档」的提取关系。

**操作步骤**：

1. 任选一个模块，例如 `modules/fifo/src/fifo.vhd`，阅读其文件头的注释段（`--` 描述）。
2. 在 [tools/build_docs.py:211-215](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L211-L215) 确认 `ModuleDocumentation` 会被这个模块调用。
3. 跑一次 `python3 tools/build_docs.py`（或在线访问 [hdl-modules.com/modules/fifo/fifo.html](https://hdl-modules.com/modules/fifo/fifo.html)）。
4. 对照网页上的 FIFO 模块说明与源码头注释，确认二者一致。

**需要观察的现象**：网页上模块页的文字说明，与 `fifo.vhd` 文件头注释的内容对应；网页上每个实体（entity）的端口表，是从 VHDL 源码的 `port` 声明自动提取的。

**预期结果**：改一处头注释后重跑，网页对应文字随之变化（待本地验证）。

**待本地验证**：若无法构建，可改为在线对照——直接看网站页面与源码头注释的对应关系。

#### 4.3.5 小练习与答案

**练习 1**：`generate_documentation` 用 `get_modules`，而 `generate_register_artifacts` 用 `get_hdl_modules`，为什么这里可以不同？
**答案**：两处都需要「所有模块的清单」。`get_hdl_modules()` 只是多了 `library_name_has_lib_suffix=False` 的库名约定；文档生成既不编译 VHDL、也不关心库名，所以直接用底层 `get_modules` 即可。功能上对「枚举模块」是等价的。

**练习 2**：为什么要 `exclude_module_folders=["rtl"]`？
**答案**：`rtl/` 放的是 netlist 构建夹具（如 `fifo_netlist_build_wrapper.vhd`，u8-l3），是测试/回归用的最小顶层 wrapper，不属于面向用户的模块接口。若不排除，文档里会多出这些无意义的实体说明，干扰读者。

---

### 4.4 Sphinx 配置与文档结构：conf.py 与 getting_started.rst

#### 4.4.1 概念说明

前面三节都在讲「怎么生成内容」，本节讲「内容长成什么样、用什么主题渲染」。这由两份手写文件决定：

- `doc/sphinx/conf.py`：Sphinx 的标准配置文件，声明扩展、主题、跨项目链接。它会被原样复制进 `generated/sphinx_rst/`（见 4.1 的复制步骤），Sphinx 编译时读取它。
- `doc/sphinx/getting_started.rst`：手写的用户指南，是网站「User guide」分组的主页。它还肩负一个重要职责——向读者讲清「怎么把 hdl-modules 接进自己的工程」，也就是「tsfpga 自动流程 vs 手动流程」两条路线。

#### 4.4.2 核心流程

Sphinx 读取 `conf.py` 后的行为大致是：

```text
build_sphinx(build_path, output_path)
  │
  ├─ 读 conf.py：
  │     extensions        → 决定启用哪些扩展（主题、sitemap、graphviz、intersphinx…）
  │     html_theme        → 决定 HTML 外观（sphinx_rtd_theme：ReadTheDocs 风格）
  │     intersphinx_mapping → 跨项目链接目标（hdl-registers/tsfpga/vunit 的官方文档）
  │     html_static_path  → 复制进 HTML 的静态资源（css/opengraph）
  │
  ├─ 解析 index.rst 的 toctree → 决定侧边栏导航结构
  ├─ 遇到 :ref:`xxx` → 解析为站内交叉引用
  ├─ 遇到 :py:meth:`... <tsfpga.module.BaseMethod...>` → 借 intersphinx 链到 tsfpga 官网
  └─ 渲染每个 .rst → .html，输出到 output_path
```

#### 4.4.3 源码精读

`conf.py` 的扩展清单见 [doc/sphinx/conf.py:30-38](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L30-L38)，这段中文说明：启用了 `sphinx_rtd_theme`（主题）、`sphinx_sitemap`（生成 sitemap.xml 利于 SEO）、`sphinx.ext.graphviz`（画图）、`sphinx.ext.intersphinx`（跨项目链接）、`sphinxcontrib.googleanalytics`（访问统计）、`sphinxext.opengraph`（社交分享卡片）、`symbolator_sphinx`（把 VHDL entity 渲染成原理图符号图）。

跨项目链接 `intersphinx_mapping` 见 [doc/sphinx/conf.py:40-44](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L40-L44)：把 `hdl_registers`、`tsfpga`、`vunit` 三个名字映射到各自的官方文档站。配合它，getting_started.rst 里写的 `:py:meth:`get_synthesis_files() <tsfpga.module.BaseModule.get_synthesis_files>``（[doc/sphinx/getting_started.rst:44-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L44-L46)）就能**直接链到 tsfpga 官网的对应 API 页**——读者点链接就跳走，本项目无需重复抄写 tsfpga 的 API 文档。这是文档生态「互链」的体现。

主题与站点配置见 [doc/sphinx/conf.py:50-69](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L50-L69)：`html_baseurl`（sitemap 基址，须以 `/` 结尾）、`html_theme="sphinx_rtd_theme"`、`html_logo`、`html_static_path=["css","opengraph"]`。注意 `conf.py` 顶部 [doc/sphinx/conf.py:18-22](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L18-L22) 同样做了 PYTHONPATH insert——因为 Sphinx 在编译时会 import 这个 `conf.py`，而它要 `from hdl_modules.about import WEBSITE_URL`，必须保证仓库根在路径上。

getting_started.rst 揭示的两条集成路线，是本节最重要的信息：

- **tsfpga 流程**（推荐）：调 `get_hdl_modules()` 把模块加进自己的模块列表，然后像普通 tsfpga 模块一样用 `get_synthesis_files()`/`get_simulation_files()`/`library_name`。寄存器 HDL、scoped constraints 都自动管好。见 [doc/sphinx/getting_started.rst:37-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L37-L49)。
- **手动流程**：不装 tsfpga 时，自己按 `src`（可综合，进仿真+构建）、`test`/`sim`（仅仿真）、`scoped_constraints`（`read_xdc -ref <实体名>` 手动加载）分类加文件；库名等于模块名；一律 VHDL-2008；寄存器代码自己跑 hdl-registers 生成。见 [doc/sphinx/getting_started.rst:51-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L51-L64) 与 [doc/sphinx/getting_started.rst:88-101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L88-L101)。

这两条路线恰好对应了 4.2 节的关键区分：走 tsfpga，寄存器代码由 `BaseModule` 自动生成；走手动，你得自己集成 hdl-registers 的 generator（getting_started.rst 的 [L114-121](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L114-L121) 正是这个要求）。

#### 4.4.4 代码实践

**实践目标**：读懂 conf.py 的扩展作用，并用 getting_started.rst 的两条路线解释「寄存器代码如何进入仿真/构建」。

**操作步骤**：

1. 在 [doc/sphinx/conf.py:30-44](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/conf.py#L30-L44) 里挑两个扩展（如 `intersphinx`、`symbolator_sphinx`），用一句话写出它们各自给网站加了什么能力。
2. 在 getting_started.rst 里找到「tsfpga 自动流程」与「手动流程」两段，填下表（源码阅读型实践）：

| 关注点 | tsfpga 流程 | 手动流程 |
| --- | --- | --- |
| 获取源码 | `get_hdl_modules()` | 手动按 `src`/`test`/`sim` 加文件 |
| 寄存器 HDL | （自动生成） | （自己跑 hdl-registers） |
| scoped constraints | （自动加载） | （`read_xdc -ref` 手动加载） |

**需要观察的现象 / 预期结果**：你能用自己的话回答 4.2 实践里的那个问题——「build_docs.py 的 generator 与仿真/构建流程里的 generator 各管什么」，并把答案挂到 getting_started.rst 的两条路线上。

**待本地验证**：表格内容来自源码阅读，无需运行即可完成。

#### 4.4.5 小练习与答案

**练习 1**：`conf.py` 里 `intersphinx_mapping` 指向 hdl-registers/tsfpga/vunit 官网，这给项目文档带来什么好处？
**答案**：项目文档里凡是引用这三个库 API 的地方（如 `:py:meth:...<tsfpga.module.BaseModule.xxx>`），都能自动渲染成指向对应官网的链接，读者一点就跳转。本项目无需复制粘贴 tsfpga 的 API 说明，避免文档与上游漂移。

**练习 2**：`conf.py` 顶部为什么也要做 `sys.path.insert` 与 `import tools.tools_pythonpath`？
**答案**：Sphinx 编译时会 import `conf.py` 当作普通 Python 模块，而 `conf.py` 里 `from hdl_modules.about import WEBSITE_URL` 依赖仓库根在 `sys.path` 上。`insert(0,...)` 优先本地检出、`tools_pythonpath` 再兜底，与所有 tools 脚本同一套引导（u1-l3）。

---

## 5. 综合实践

把本讲所有知识点串起来：**追踪一条「寄存器 toml → 文档展示 → 仿真/构建使用」的完整链路**。

以 `dma_axi_write_simple` 模块为例（你已在 u7-l2、u7-l3 熟悉它）：

1. **数据源**：确认 `modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml` 存在，它是 `module.registers` 的来源（tsfpga `BaseModule` 加载时解析）。
2. **文档侧**：在 [tools/build_docs.py:132-165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_docs.py#L132-L165) 找到这个模块会被哪 7 个 generator 处理，写出每个 generator 产出的文件名与去向目录（`generated/sphinx_rst/modules/dma_axi_write_simple/...`）。
3. **构建侧**：阅读 [modules/dma_axi_write_simple/module_dma_axi_write_simple.py:31-90](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/module_dma_axi_write_simple.py#L31-L90) 的 `get_build_projects`，确认它把 `modules=get_hdl_modules()` 传进 `TsfpgaExampleVivadoNetlistProject`——也就是说，构建时 tsfpga 会自动为这些模块生成寄存器 VHDL 并加入综合，**这条路径与 build_docs.py 无关**。
4. **对照结论**：用一段话说明「文档网站展示的 dma 寄存器 VHDL 包」与「Vivado 综合时实际用的 dma 寄存器 VHDL 包」是同一套 generator、同一份 toml 的两份独立产物，前者在 `generated/sphinx_rst/`（展示），后者在 tsfpga 即时生成（综合）。

> 进阶（可选）：跑一次 `python3 tools/build_docs.py`，在 `generated/sphinx_rst/modules/dma_axi_write_simple/vhdl/` 下找到生成的寄存器包文件，与 u7-l3 提到的手写 VHDL 里 `use` 的寄存器包名对照，确认命名一致。待本地验证。

## 6. 本讲小结

- `tools/build_docs.py` 是**一个脚本搞定整个网站**：生成 release notes、BibTeX、寄存器代码、模块 RST，再跑 Sphinx、画徽章，五阶段流水线全部产物落 `generated/`，不污染源码树。
- 寄存器代码生成集中在 `generate_register_artifacts()`：对每个有 `module.registers` 的模块跑 4 个 VHDL generator + 3 个 C++ generator，产物用于**网站展示**；这与仿真/构建流程里 tsfpga `BaseModule` **自动**生成的代码是两条独立路径，但共用同一套 generator 与 toml。
- 模块文档由 tsfpga `ModuleDocumentation` 自动提取源码头注释与寄存器表生成，`rtl/`（netlist 夹具）被显式排除；`index.rst` 用三个 toctree 组织成 About / User guide / Modules 导航。
- README 在 GitHub 与网站两处维护，`get_readme()` 用逐字节比对做一致性护栏，漂移即构建失败。
- `conf.py` 通过 extensions（含 intersphinx、symbolator）与 `sphinx_rtd_theme` 决定网站能力与外观；intersphinx 让本项目能直接链到 tsfpga/hdl-registers/vunit 官网，避免重复维护上游 API 文档。
- `getting_started.rst` 把「集成 hdl-modules」明确分成 **tsfpga 自动流程**（寄存器/约束全自动）与**手动流程**（自己跑 generator、自己 `read_xdc`），这正解释了 4.2 的核心区分。

## 7. 下一步学习建议

- **u9-l2（Netlist 综合与 FPGA 构建流程）**：从「文档生成」转向「综合/构建」，精读 `tools/synthesize.py` 与 `tools/build_fpga.py`，看 tsfpga 如何在构建时即时生成寄存器 HDL 并喂给 Vivado——正好闭合本讲 4.2 节留下的「构建侧自动生成」那条路。
- **u9-l3（发布工程、Lint 与贡献规范）**：了解 `tag_release.py` 与 release notes 的维护约定，以及 CI 里如何把 `build_docs.py` 跑成网站部署流水线。
- **延伸阅读**：装好依赖后，自己给某个模块（如 `math`）的某个 entity 头注释加一句话，重跑 `build_docs.py`，观察对应网页变化，体会「源码即文档」的工作流。
