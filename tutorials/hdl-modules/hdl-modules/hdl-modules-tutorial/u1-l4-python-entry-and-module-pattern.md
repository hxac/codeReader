# Python 入口与 tsfpga Module 模式

## 1. 本讲目标

前几讲我们看清了「hdl-modules 是什么」「仓库长什么样」「要装哪些依赖」。本讲要回答最后一个导览问题：**这些 VHDL 模块，是怎么被 Python 工具链发现、并自动接上仿真与综合流程的？**

学完本讲你应该能够：

- 说清 `hdl_modules/__init__.py` 里的 `get_hdl_modules()` 是干什么的、扫描了哪里。
- 解释每个模块目录里那个 `module_*.py` 文件的作用，以及它继承的 `BaseModule` 提供了哪些「钩子（hook）」。
- 区分 `setup_vunit` 和 `get_build_projects` 这两个钩子各自被谁调用、在什么时机调用。
- 看懂「用嵌套循环 + `add_vunit_config` 生成 generic 组合矩阵」这一全项目统一的仿真配置写法。
- 理解 `get_build_projects` 如何用 netlist 构建把资源占用纳入回归断言。

本讲是后续所有「读某个具体模块」讲义的基础——后面你会反复看到 `module_*.py` 这个文件，所以必须先在这里把它彻底吃透。

## 2. 前置知识

本讲主要涉及 Python 与工程组织，不涉及具体 VHDL 语法，但需要你已经接受以下概念（在 u1-l1～u1-l3 已建立）：

- **VHDL-2008 / generic（类属参数）**：VHDL 实体在实例化时可以传参，例如 `width=>32`、`enable_last=>true`。同一个实体靠不同 generic 取值长成不同电路。
- **库名等于模块名**：u1-l2 讲过的硬约定，引用某个模块只需 `library fifo;`，没有 `lib` 后缀。这一点在源码里就由 `library_name_has_lib_suffix=False` 决定。
- **src/test/sim 三类目录**：`src/` 进综合和仿真，`test/`（测试台）与 `sim/`（BFM）只进仿真。本讲讲的 `module_*.py` 不属于这三类，它是「工程元数据」，由 Python 工具链读取。
- **tsfpga / VUnit**：u1-l3 讲过的两个核心依赖。VUnit 负责跑仿真；tsfpga 在它之上提供「模块扫描、库管理、约束施加、Vivado 构建」等工程化能力。
- **钩子（hook）模式**：父类（这里是 tsfpga 的 `BaseModule`）定义一组「在某个时机会被调用」的方法，子类按需重写它们来插入自己的逻辑。本讲的 `setup_vunit`、`get_build_projects` 就是两个钩子。

如果你对 Python 的 `from x import y`、`Path(__file__)`、`**kwargs`、`@staticmethod`、`dataclass` 这些写法完全不熟，建议先补一下基础再往下读，但它们都不复杂，结合上下文也能看懂大概。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [hdl_modules/__init__.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py) | Python 包入口，提供 `get_hdl_modules()`——把 `modules/` 目录扫描成一组 Module 对象的公共 API。 |
| [modules/fifo/module_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py) | fifo 模块的「工程元数据」：定义仿真如何配置、综合如何构建。是本讲的主力示例。 |
| [modules/common/module_common.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py) | common 模块的元数据，结构更复杂，是代码实践任务的解剖对象。 |
| [tools/simulate.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py) | 仿真入口脚本，是触发 `setup_vunit` 的地方。 |
| [tools/build_fpga.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py) | FPGA 构建入口脚本，是触发 `get_build_projects` 的地方。 |

记住一条总线索：**VHDL 源码（`.vhd`）描述电路，而 `module_*.py` 描述「这些电路怎么被测、怎么被综合」**。两者分工明确，互不混杂。

## 4. 核心概念与源码讲解

### 4.1 Python 包入口：get_hdl_modules()

#### 4.1.1 概念说明

hdl-modules 既是「一堆 VHDL 文件」，也是一个可 `import` 的 Python 包（包名就叫 `hdl_modules`，见仓库根目录的 `hdl_modules/` 文件夹）。这个包对外的核心功能只有一个：**告诉调用者「我这个项目里一共有哪些模块」**。

这件事由 `get_hdl_modules()` 完成。它返回一个 `ModuleList`——也就是一组「模块对象」。每个模块对象封装了一个模块目录（如 `modules/fifo/`）的全部工程信息：库名、源码文件列表、约束文件、以及那个 `module_*.py` 里定义的 `Module` 类。

为什么要单独做一层 Python 包装，而不让大家直接去翻 `modules/` 目录？因为「扫描目录、按约定找到 `module_*.py`、实例化其中的 `Module` 类、收集源码与约束」这套逻辑比较繁琐，tsfpga 已经实现了，hdl-modules 只需用正确的参数调用它，并固化自己的约定（库名无 `lib` 后缀）。这样所有用户——无论你是跑自带工具、还是在自己项目里 `import hdl_modules`——拿到的都是同一份、行为一致的模块清单。

#### 4.1.2 核心流程

`get_hdl_modules()` 的工作流程可以用下面这串伪代码概括：

```
get_hdl_modules(names_include, names_avoid):
    # 延迟导入 tsfpga（见 4.1.3 解释为何不放在文件顶部）
    from tsfpga.module import get_modules

    return get_modules(
        modules_folder = <仓库根>/modules,
        names_include  = 用户想要的白名单,   # 可选
        names_avoid    = 用户想排除的黑名单,  # 可选
        library_name_has_lib_suffix = False, # 固化约定：库名不带 lib 后缀
    )
```

tsfpga 的 `get_modules` 内部会：

1. 列出 `modules_folder` 下的每个子目录（`fifo`、`common`、`resync`…）。
2. 在每个子目录里寻找 `module_<目录名>.py`（例如 `modules/fifo/module_fifo.py`）。
3. 实例化该文件里名为 `Module` 的类，得到一个模块对象。
4. 用 `names_include` / `names_avoid` 做过滤。
5. 返回 `ModuleList`。

其中「仓库根」由同一文件里的 `REPO_ROOT` 推导出来，保证无论你在哪台机器、哪个路径下调用，扫描的始终是「这个仓库自己的 `modules/`」。

#### 4.1.3 源码精读

先看包入口的整体：

[hdl_modules/__init__.py:20-25](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py#L20-L25) —— 用 `Path(__file__).parent.parent` 推导仓库根目录，并定义版本号与文档字符串（标语来自 `about.py` 的单一信息源，u1-l1 讲过）。

接着是核心函数：

[hdl_modules/__init__.py:28-50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py#L28-L50) —— `get_hdl_modules()` 的全部实现。注意三个细节：

1. **延迟导入**：`from tsfpga.module import get_modules` 写在函数体里（第 43 行），而不是文件顶部。注释解释了原因——有些使用 hdl-modules 的机器上并没有装 tsfpga（比如只拿 src/ 里的可综合源码、不跑测试的人）。如果放在顶部，`import hdl_modules` 就会因为找不到 tsfpga 而直接报错；放进函数体后，只有真正调用 `get_hdl_modules()` 的人才需要装 tsfpga。
2. **固化约定**：第 49 行 `library_name_has_lib_suffix=False`，这正是 u1-l2 讲的「库名等于模块名、无 `lib` 后缀」在代码里的落点。
3. **过滤参数透传**：`names_include` / `names_avoid` 原样传给 tsfpga，hdl-modules 不自己实现过滤逻辑。

> 补充说明：仓库里另两个入口脚本 [tools/simulate.py:35](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L35) 和 [tools/build_fpga.py:30](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py#L30) 直接调用的是 `tsfpga.module.get_modules(modules_folder=tools_env.HDL_MODULES_DIRECTORY)`，而不是 `get_hdl_modules()`。二者扫描的是同一个 `modules/` 目录、效果一致，区别只在于：自带工具已经在 tsfpga 命名空间里，直接用底层函数最自然；而 `get_hdl_modules()` 是面向「`import hdl_modules` 的外部用户」以及 `module_*.py` 内部嵌套引用（见 4.4.3）的便捷封装。

#### 4.1.4 代码实践

这是一个源码阅读型实践，目标是验证「扫描结果与目录一一对应」。

1. **实践目标**：确认 `get_hdl_modules()` 返回的模块清单和 `modules/` 目录完全对应。
2. **操作步骤**：
   - 在仓库根目录配好 `PYTHONPATH`（参考 u1-l3，让 `import hdl_modules` 与 `import tsfpga` 都能成功）。
   - 启动 Python，执行：
     ```python
     from hdl_modules import get_hdl_modules
     mods = get_hdl_modules()
     print(sorted(m.name for m in mods))
     ```
   - 对照 `modules/` 下的子目录数量与名称。
3. **需要观察的现象**：打印出的名称应与 `modules/` 下的子目录一一对应（axi、axi_lite、axi_stream、bfm、common、dma_axi_write_simple、fifo、hard_fifo、lfsr、math、register_file、resync、ring_buffer、sine_generator）。
4. **预期结果**：共 14 个模块名，与 u1-l1 通报的 14 个模块一致。再试一次 `get_hdl_modules(names_include={"fifo"})`，应只返回 fifo 一个模块，验证白名单过滤生效。
5. 如果本地尚未装好 tsfpga/VUnit，**待本地验证**——可改为先用文件浏览器数 `modules/*/module_*.py` 的个数（应为 14）来交叉确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `from tsfpga.module import get_modules` 要写在函数体内，而不能放到文件顶部的 import 区？

**参考答案**：因为有些只用 src/ 可综合源码的用户并没有安装 tsfpga。放在顶部会让 `import hdl_modules` 在这些机器上直接失败；放进函数体后，只有真正调用 `get_hdl_modules()` 的人（他们必然有 tsfpga）才会触发这次导入。

**练习 2**：`library_name_has_lib_suffix=False` 这一行去掉会发生什么？

**参考答案**：tsfpga 默认会给库名加 `lib` 后缀（如 `fifo_lib`）。hdl-modules 的所有 VHDL 都按「库名等于模块名（`library fifo;`）」书写，所以必须显式关掉后缀，否则综合/仿真时会找不到正确的库。

---

### 4.2 tsfpga Module 模式：module_*.py 与 BaseModule

#### 4.2.1 概念说明

`get_hdl_modules()` 只是「找到模块」，真正让每个模块「接入流程」的，是模块目录里的那个 `module_*.py` 文件。它的命名是固定约定：**文件名必须是 `module_<模块名>.py`**（如 `module_fifo.py`、`module_common.py`），并且文件里要定义一个名字就叫 `Module` 的类。

这个类继承自 tsfpga 提供的 `BaseModule`。`BaseModule` 已经实现了大量通用能力（扫描 src/test/sim、收集约束、解析寄存器定义等），并暴露出几个**钩子方法**供子类重写。hdl-modules 里最重要的两个钩子是：

- `setup_vunit(vunit_proj, **kwargs)`：**仿真钩子**。让模块把自己「要跑哪些测试、每个测试用哪几组 generic」登记到 VUnit 工程里。
- `get_build_projects()`：**综合钩子**。返回这个模块要进行哪些 netlist / FPGA 构建，以及每份构建期望的资源占用。

这种「继承基类 + 重写钩子」的模式，就是全项目 14 个 `module_*.py` 的统一写法。你只要看懂 fifo 这一个，其余十三个都是同一套套路。

#### 4.2.2 核心流程

一个 `module_*.py` 的生命周期分两条互不干扰的路径：

```
路径 A（仿真）：tools/simulate.py
   -> SimulationProject.add_modules(modules)
   -> tsfpga 对每个 Module 回调 setup_vunit(vunit_proj)
   -> vunit_proj.main() 跑测试

路径 B（综合）：tools/build_fpga.py
   -> tsfpga.get_build_projects(modules, ...)
   -> tsfpga 对每个 Module 回调 get_build_projects()
   -> 返回的工程列表交给 Vivado 综合
```

关键点：**两个钩子由 tsfpga 在不同入口里回调，互不依赖**。跑仿真时不会触发 `get_build_projects`，跑综合时也不会触发 `setup_vunit`。这也是为什么 `module_*.py` 可以放心地把这两类逻辑分开写。

`BaseModule` 还提供几个子类会反复用到的辅助方法/属性：

- `self.name` / `self.library_name`：模块名与对应的 VHDL 库名（无 `lib` 后缀，故二者相等）。
- `self.add_vunit_config(test, generics=..., count=...)`：把「某个测试 + 一组 generic」登记为一条 VUnit 配置。
- `self.netlist_build_name(entity, generics)`：按「实体名 + generic」生成一个标准化、可读的 netlist 构建工程名。

#### 4.2.3 源码精读

先看 fifo 模块 `Module` 类的骨架与导入：

[modules/fifo/module_fifo.py:14-32](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L14-L32) —— 从 tsfpga 导入 `BaseModule`、netlist 工程类 `TsfpgaExampleVivadoNetlistProject`，以及一组资源检查器（`EqualTo`、`Ffs`、`TotalLuts` 等）；随后 `class Module(BaseModule)`。注意类名必须叫 `Module`，这是 tsfpga 扫描时的硬约定。

再看两个钩子的签名：

[modules/fifo/module_fifo.py:33-37](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L33-L37) —— `setup_vunit` 的签名。它接收 tsfpga 传来的 `vunit_proj`（一个 VUnit 工程对象），`**kwargs` 用 `# noqa: ARG002` 表示「故意忽略额外参数」，因为基类签名预留了扩展位，但 fifo 用不到。

[modules/fifo/module_fifo.py:116-130](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L116-L130) —— `get_build_projects` 的签名与总体结构。它返回一个工程列表，内部把具体构造委托给两个私有方法 `_setup_fifo_build_projects` 和 `_setup_asynchronous_fifo_build_projects`（详见 4.4）。

至于「谁调用这两个钩子」，证据在自带工具里：

[tools/simulate.py:37-38](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L37-L38) —— `SimulationProject(...)` 建好后调用 `add_modules(modules=modules)`；tsfpga 在添加每个模块时会回调该模块的 `setup_vunit`。最终 [tools/simulate.py:55](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py#L55) 的 `vunit_proj.main()` 才真正跑测试。

[tools/build_fpga.py:31-37](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py#L31-L37) —— `get_build_projects(modules=modules, ...)` 来自 `tsfpga.build_project_list`，它遍历每个模块、调用模块的 `get_build_projects()`，把所有工程汇总成 `project_list` 交给后续 Vivado 流程。

> 小结：`setup_vunit` 的调用者是 **tsfpga 的仿真工程**，时机是 **跑 `tools/simulate.py` 时、`add_modules` 阶段**；`get_build_projects` 的调用者是 **tsfpga 的构建列表**，时机是 **跑 `tools/build_fpga.py` 时、收集工程阶段**。

#### 4.2.4 代码实践

1. **实践目标**：确认「类名必须叫 `Module`、文件名必须叫 `module_<名>.py`」这条约定在全项目成立。
2. **操作步骤**：在仓库根目录列出所有 `module_*.py`，逐个核对文件名与所在目录名是否一致。
3. **需要观察的现象**：每个模块目录下有且仅有一个 `module_<目录名>.py`。
4. **预期结果**：共 14 个文件（axi、axi_lite、axi_stream、bfm、common、dma_axi_write_simple、fifo、hard_fifo、lfsr、math、register_file、resync、ring_buffer、sine_generator），文件名与目录名一一对应。
5. 这是静态阅读，无需运行；若想进一步验证，可在 Python 里 `import` 某个 `module_*.py`，检查其 `Module` 是否为 `tsfpga.module.BaseModule` 的子类（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：如果我新建一个模块目录 `modules/foo/`，里面的元数据文件该叫什么名字、类该叫什么名字？

**参考答案**：文件必须叫 `module_foo.py`，里面必须定义一个名为 `Module` 且继承 `BaseModule` 的类。否则 tsfpga 扫描时找不到它，该模块就不会出现在 `get_hdl_modules()` 的返回值里。

**练习 2**：为什么 `setup_vunit` 和 `get_build_projects` 要拆成两个钩子，而不是合到一个方法里？

**参考答案**：因为仿真和综合是两条独立的流水线，由不同入口（`simulate.py` vs `build_fpga.py`）在不同时机触发。拆开之后，跑仿真时不会白白去构造一堆 Vivado 工程，跑综合时也不会去登记测试配置，职责清晰、开销最小。

---

### 4.3 setup_vunit：用 generic 矩阵批量配置仿真

#### 4.3.1 概念说明

hdl-modules 对测试覆盖非常执着（u1-l1 讲过「质量被置于首位」）。一个 FIFO 实体因为有大量 generic（`enable_last`、`enable_packet_mode`、`enable_output_register`、`depth`…），单跑一次测试远远不够——你需要把各种 generic 组合都过一遍。这就叫 **generic 矩阵（generic matrix）**。

`setup_vunit` 的核心职责，就是用嵌套循环枚举出这些组合，再调用 `self.add_vunit_config(test, generics=...)` 把每一组都登记成一条独立的 VUnit 测试配置。最终 VUnit 会对每条配置各跑一次。

这种写法的好处：用十几行 Python 就能展开成几十甚至上百条测试，既保证了覆盖，又不需要手写一堆测试台。

#### 4.3.2 核心流程

fifo 的 `setup_vunit` 套路是这样的：

```
for test in library.test_bench("tb_asynchronous_fifo").get_tests():
    for enable_output_register in [False, True]:          # 维度 1
        for read_clock_is_faster in [False, True]:        # 维度 2
            generics = { ...这两个维度的取值... }
            for generics in self.generate_common_fifo_test_generics(test.name, generics):
                # 维度 3：按测试名再补一组 generic（depth、almost level 等）
                self.add_vunit_config(test, generics=generics)
```

这里有三层维度：两个布尔 generic（`enable_output_register`、`read_clock_is_faster`）的笛卡尔积，再叠加一个按测试名定制的 `generate_common_fifo_test_generics`。后者是一个 `@staticmethod`，它根据测试名里是否包含 `packet_mode`、`drop_packet`、`peek_mode`、`init_state` 等关键词，决定再补哪些 generic、以及 `yield` 出几组 depth 配置。

配置总数大概是各维度取值数的乘积。以最简单情况为例，两个布尔维度各 2 个取值，depth 维度 2 组，则单个测试大约展开为：

\[
N_{\text{config}} \approx 2 \times 2 \times 2 = 8
\]

不同测试名会因 `generate_common_fifo_test_generics` 的分支不同而展开数略有差异，这正是「按测试名定制」的灵活之处。

当模块有很多测试台时（common 模块有十几个），`setup_vunit` 会变得很长。fifo 把 fifo 专属逻辑抽成 `generate_common_fifo_test_generics`；而 common 模块则更进一步，把每个测试台的配置逻辑各自拆到一个 `_setup_*_tests` 私有方法里，`setup_vunit` 只负责「分发」——这也是本讲代码实践的解剖对象。

#### 4.3.3 源码精读

先看 fifo 的完整 `setup_vunit`：

[modules/fifo/module_fifo.py:38-63](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L38-L63) —— 两个测试台（`tb_asynchronous_fifo`、`tb_fifo`）各用一层嵌套循环枚举 generic，再调 `add_vunit_config`。注意第 60-61 行有个典型写法：当 `enable_output_register` 且测试名含 `peek_mode` 时 `continue` 跳过——因为该组合在硬件上不支持。这种「在循环里用 continue 过滤非法组合」是项目里反复出现的模式。

辅助函数 `generate_common_fifo_test_generics`：

[modules/fifo/module_fifo.py:65-114](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L65-L114) —— 它是个**生成器**（用 `yield`），按测试名关键词决定补哪些 generic，并 `yield` 出 1～2 组 depth 配置。用生成器而非返回列表，是为了让调用方在 `for ... in ...` 里自然地展开多组配置。

再看 common 模块如何把这一套规模化：

[modules/common/module_common.py:33-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L33-L49) —— common 的 `setup_vunit` 不写任何循环，而是连续调用 12 个 `_setup_*_tests` 私有方法。每个私有方法负责一个测试台（见下例），把「按测试名定制 generic」的细节封装在各自方法里，主入口保持清爽。

随便挑两个私有方法看写法：

[modules/common/module_common.py:75-94](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L75-L94) —— `_setup_clock_counter_tests` 直接登记两条固定 generic；`_setup_event_aggregator_tests` 则遍历测试、按测试名把 `tick_count`/`event_count` 设成不同值。这两种写法（固定枚举 vs 按名定制）在项目里都很常见。

[modules/common/module_common.py:122-148](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L122-L148) —— `_setup_handshake_pipeline_tests` 用 `itertools.product` 把三个布尔 generic 做笛卡尔积，再用两个 `continue` 过滤掉「硬件不支持」或「测试名不匹配」的组合。这是 generic 矩阵最典型的写法。

#### 4.3.4 代码实践（本讲主实践）

这就是本讲规格里要求的实践任务。

1. **实践目标**：在 `module_common.py` 中找出 `setup_vunit` 调用了哪些 `_setup_*_tests` 方法，并用自己的话解释 `setup_vunit` 与 `get_build_projects` 各自被谁调用、何时调用。
2. **操作步骤**：
   - 打开 [modules/common/module_common.py:33-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L33-L49)，把 12 个 `_setup_*_tests` 方法名抄下来。
   - 任选其中两个（建议选 `_setup_clock_counter_tests` 和 `_setup_handshake_pipeline_tests`），读它们的循环结构，说出各自枚举了哪些 generic、有没有用 `continue` 过滤非法组合。
   - 打开 [tools/simulate.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py) 与 [tools/build_fpga.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py)，定位 `add_modules` 与 `get_build_projects` 的调用点。
3. **需要观察的现象**：
   - `setup_vunit` 里共有 12 行 `_setup_*_tests(...)`，每行对应一个测试台。
   - `get_build_projects`（[module_common.py:51-73](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L51-L73)）里则是 9 个 `_get_*_build_projects(...)`，结构和 `setup_vunit` 类似但服务于综合。
4. **预期结果（参考答案）**：
   - `setup_vunit` 调用的 12 个方法是：`_setup_clock_counter_tests`、`_setup_event_aggregator_tests`、`_setup_clean_packet_dropper_tests`、`_setup_debounce_tests`、`_setup_handshake_merger_tests`、`_setup_handshake_mux_tests`、`_setup_handshake_pipeline_tests`、`_setup_handshake_splitter_tests`、`_setup_keep_remover_tests`、`_setup_periodic_pulser_tests`、`_setup_strobe_on_last_tests`、`_setup_width_conversion_tests`。
   - **`setup_vunit` 被谁调用、何时**：由 tsfpga 的仿真工程在运行 `tools/simulate.py` 时、执行 `simulation_project.add_modules(modules)` 阶段回调；它负责把 common 模块每个测试台的 generic 组合登记进 VUnit。
   - **`get_build_projects` 被谁调用、何时**：由 tsfpga 的构建列表在运行 `tools/build_fpga.py` 时、执行 `get_build_projects(modules=...)` 阶段回调；它负责返回 common 模块要进行哪些 netlist 综合及其资源断言。
5. 这一步是纯源码阅读，不需要跑工具即可完成；若想实测，可在配好依赖后运行 `python tools/simulate.py --list`（**待本地验证**该参数是否为本仓库支持的写法）查看 common 模块展开出的测试条目数。

#### 4.3.5 小练习与答案

**练习 1**：`generate_common_fifo_test_generics` 为什么用 `yield` 而不是 `return` 一个列表？

**参考答案**：因为它要为同一个测试「按测试名」产出 1 组或 2 组 depth 配置。用生成器后，调用方一行 `for generics in self.generate_common_fifo_test_generics(...)` 就能自然遍历所有组；如果返回列表，调用方还得再写一层解包。生成器让「枚举维度」可以像流水线一样叠加。

**练习 2**：`_setup_handshake_pipeline_tests` 里为什么有两个 `continue`？

**参考答案**：第一个 `continue` 过滤掉硬件不支持的组合（只流水线控制信号却要求满吞吐）；第二个 `continue` 保证名为 `full_throughput` 的测试只在 `full_throughput=True` 时运行。用 `continue` 在循环里剔除非法/无意义组合，是 generic 矩阵里标准的「安全阀」写法。

---

### 4.4 get_build_projects：netlist 构建与资源回归断言

#### 4.4.1 概念说明

`setup_vunit` 关心「功能对不对」，`get_build_projects` 则关心「综合出来有多大、时序怎么样」。它返回一组构建工程对象，每个工程指定：

- **顶层实体**（`top`）与一组 **generic**；
- **参与综合的模块集合**（`modules`）；
- 目标 FPGA **型号**（`part`）；
- 一组 **资源检查器**（`build_result_checkers`），用来断言综合后的 LUT / FF / BRAM / 逻辑级数等指标。

这些工程绝大多数是 **netlist 构建**（综合到网表即停，不做布局布线），用来快速反馈「我这个改动有没有让 FIFO 多花 LUT」。把每份构建期望的资源数写成断言，就等于把资源占用纳入了 CI 回归——一旦某次改动让面积变大，断言失败，CI 立刻报警。这正是 hdl-modules「面积优先优化」哲学（u1-l1）在工程层面的落地。

#### 4.4.2 核心流程

```
get_build_projects(self):
    projects = []
    modules  = get_hdl_modules(names_include=[self.name, "common", "math", "resync"])
    part     = "xc7z020clg400-1"

    self._setup_fifo_build_projects(projects, modules, part)              # 同步 FIFO
    self._setup_asynchronous_fifo_build_projects(projects, modules, part) # 异步 FIFO
    return projects
```

每个私有方法里反复出现的构造单元是：

```
projects.append(
    TsfpgaExampleVivadoNetlistProject(
        name=self.netlist_build_name("fifo.minimal", generics),  # 标准化工程名
        modules=modules,            # 哪些模块参与综合
        part=part,                  # FPGA 型号
        top="fifo_netlist_build_wrapper",  # 顶层实体
        generics=generics,          # 本次 generic 取值
        build_result_checkers=[     # 资源断言
            TotalLuts(EqualTo(14)),
            Ffs(EqualTo(24)),
            Ramb36(EqualTo(1)),
            Ramb18(EqualTo(0)),
            MaximumLogicLevel(EqualTo(6)),
        ],
    )
)
```

注意 `build_result_checkers` 里 `EqualTo(14)` 这种写法：它断言「这个工程综合后 LUT 数必须正好等于 14」。这是非常严格的回归——连「多花 1 个 LUT」都会被发现。注释里会解释为什么某项 generic 会让资源增加或不变（例如「开 `enable_last` 不应增加资源」「开 `enable_packet_mode` 会引入额外计数器」），这些注释本身就是学习「generic 如何影响面积」的好材料。

#### 4.4.3 源码精读

看 fifo 的 `get_build_projects` 主体：

[modules/fifo/module_fifo.py:116-130](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L116-L130) —— 注意第 121 行又一次出现延迟导入 `from hdl_modules import get_hdl_modules`，注释解释：`get_build_projects` 只在仓库内跑 `tools/build_fpga.py` 时被调用，那时 `PYTHONPATH` 已正确设置，所以可以放心 `import hdl_modules`；而普通用户 `import` 该 `module_*.py` 时不会触发这里。第 124 行用 `names_include=[self.name, "common", "math", "resync"]` 只拉取 fifo 综合所需的依赖模块，避免把全仓库都纳入工程。

看一份具体的 netlist 工程定义：

[modules/fifo/module_fifo.py:132-158](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L132-L158) —— 「最小 FIFO」配置：顶层用 `fifo_netlist_build_wrapper`（一个只引出最基本端口的封装夹具，u1-l2 提过的 `rtl/` 目录产物），generic 关掉所有可选特性，断言 `TotalLuts(EqualTo(14))`、`Ramb36(EqualTo(1))` 等。注释说明用 wrapper 是为了得到「最精简」的 FIFO 资源数。

[modules/fifo/module_fifo.py:200-237](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L200-L237) —— 这一段是学习「generic → 面积」关系的好例子：注释分别说明「开 `enable_last` 不应增加资源」「开 `enable_packet_mode` 会因额外计数器而增加资源」，断言数字也随之变化（LUT 从 27 涨到 40）。

common 模块的 `get_build_projects` 采用与 `setup_vunit` 一样的分发风格：

[modules/common/module_common.py:51-73](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L51-L73) —— 主入口只负责调用 9 个 `_get_*_build_projects`，把「每个实体的资源断言」封装在各自方法里。

最后看一个特别情况：

[modules/common/module_common.py:581-592](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L581-L592) —— `_get_time_pkg_build_projects` 没有传 `build_result_checkers`，注释解释：这个顶层「不检查资源，但实体内部含大量 assertion」。也就是说，netlist 构建也能被借用为「让 Vivado 把 VHDL assertion 真正综合进去并跑起来」的手段，不只是为了查面积。

#### 4.4.4 代码实践

1. **实践目标**：理解「generic 取值如何映射到断言的资源数」，并尝试为某个 generic 预测面积变化。
2. **操作步骤**：
   - 阅读 [modules/fifo/module_fifo.py:200-300](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L200-L300)，把 `enable_last` → `enable_packet_mode` → `enable_output_register` → `enable_drop_packet` → `enable_peek_mode` 这条递增链上，每一步断言的 `TotalLuts` 数值列成表格。
   - 对照每段顶部的注释，验证「注释解释的原因」与「数字的变化方向」是否一致。
3. **需要观察的现象**：资源数随特性逐个开启而单调（或近单调）上升，注释给出的原因（额外计数器、额外地址指针等）能解释每一次跳变。
4. **预期结果**：例如 `fifo.with_levels`（27 LUT）→ 开 `enable_last` 仍为 27（不变）→ 开 `enable_packet_mode` 涨到 40 → 再开 output register 涨到 45；`asynchronous_fifo` 链路上还能看到「开 `enable_drop_packet` 反而减少资源」的反直觉现象，注释解释是省掉了一个 `resync_counter`。
5. 进阶（**待本地验证**）：若本地装好 Vivado，可运行 `python tools/build_fpga.py --netlist-builds --project-filter fifo.minimal` 之类命令，实测 LUT 数是否与断言一致；命令确切写法以 `python tools/build_fpga.py --help` 为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `get_build_projects` 里要 `names_include=[self.name, "common", "math", "resync"]`，而不是把全部 14 个模块都加进工程？

**参考答案**：综合一个工程只需要被测实体及其依赖模块。fifo 依赖 common、math、resync，把无关模块也加进来既拖慢综合，也可能引入库名冲突。用白名单精确圈定依赖，是「最小工程」原则。

**练习 2**：`build_result_checkers` 用 `EqualTo(...)`（精确等于）而不是「小于等于」，这样会不会太严格？

**参考答案**：是有意为之的严格。hdl-modules 把资源占用当作回归指标，要求每次改动后面积**完全不变**（改进时应主动更新断言数字）。这样任何导致面积意外增加的改动都会立刻被 CI 拦下，迫使作者正视资源代价，契合「面积优先」哲学。

---

## 5. 综合实践

把本讲四节的内容串起来，完成下面这个「迷你模块」任务（纯设计 + 源码阅读，不要求真的提交代码）：

> 假设你要给 `modules/common/` 里新增一个实体 `foo`（带一个 `width` generic 和一个测试台 `tb_foo`），请描述你会**修改哪两个文件、各加什么内容**，让它同时具备「仿真配置」和「资源回归」。

要求：

1. 说出你会动 `module_common.py` 的哪两个钩子，分别在哪个分发方法里追加调用。
2. 写出 `setup_vunit` 侧新增的 `_setup_foo_tests` 的伪代码：用嵌套循环枚举 `width` 取 `[8, 16, 32]`，并对每个测试调用 `self.add_vunit_config`。
3. 写出 `get_build_projects` 侧新增的 `_get_foo_build_projects` 的伪代码：构造一个 `TsfpgaExampleVivadoNetlistProject`，`top="foo"`，并带上 `TotalLuts(EqualTo(...))` 之类的检查器（数字先用占位符，标注「待综合后填入」）。
4. 解释：如果只完成了第 2 步、没做第 3 步，你的改动会在哪条 CI 流水线上体现、哪条上完全无感？

参考思路：

- 仿真侧在 [modules/common/module_common.py:33-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L33-L49) 的 `setup_vunit` 里加一行 `self._setup_foo_tests(vunit_proj=vunit_proj)`，并实现该私有方法（套路参考 `_setup_clean_packet_dropper_tests`，[module_common.py:96-100](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L96-L100)）。
- 综合侧在 [modules/common/module_common.py:51-73](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L51-L73) 的 `get_build_projects` 里加一行 `self._get_foo_build_projects(part, projects)`，套路参考 `_get_strobe_on_last_build_projects`（[module_common.py:552-579](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L552-L579)）。
- 只做仿真侧：跑 `tools/simulate.py` 时会出现 `tb_foo` 的多条配置；跑 `tools/build_fpga.py` 时完全无感（没有对应的 netlist 工程，也就没有资源断言）。

这个练习如果真的上手写，还需要新建 `modules/common/src/foo.vhd` 与 `modules/common/test/tb_foo.vhd`——但那是后续「读具体模块」讲义的内容，本讲只聚焦 Python 侧的工程接线。

## 6. 本讲小结

- `hdl_modules/__init__.py` 的 `get_hdl_modules()` 是项目对外的 Python 入口，它用 `library_name_has_lib_suffix=False` 调用 tsfpga 的 `get_modules`，把 `modules/` 扫描成一组 Module 对象；为兼容「没装 tsfpga 的用户」，关键导入做了延迟处理。
- 每个模块目录里的 `module_<名>.py` 定义一个继承 `BaseModule` 的 `Module` 类，文件名与类名都是硬约定（全项目共 14 个）。
- `setup_vunit` 是仿真钩子，由 tsfpga 在 `tools/simulate.py` 的 `add_modules` 阶段回调；它用嵌套循环 + `self.add_vunit_config` 生成 generic 矩阵。
- `get_build_projects` 是综合钩子，由 tsfpga 在 `tools/build_fpga.py` 收集工程时回调；它返回一组 netlist 工程，并用 `build_result_checkers`（`EqualTo`）把资源占用纳入回归。
- `BaseModule` 提供的 `self.library_name`、`self.add_vunit_config`、`self.netlist_build_name` 是 `module_*.py` 里最常用的三件套。
- common 模块把这两类逻辑分别拆成 `_setup_*_tests`（12 个）与 `_get_*_build_projects`（9 个）两组私有方法做分发，是规模化后最清爽的写法。

## 7. 下一步学习建议

本讲是「导览单元（u1）」的最后一讲，到此你已经具备了从 Python 工具链视角理解整个项目的能力。接下来建议：

- **进入 u2 打地基**：先读 [u2-l1 握手约定](u2-l1-handshake-convention.md)，理解 ready/valid 握手——这是 fifo、axi_stream 等绝大多数模块的接口基础。
- **想立刻看一个真实模块怎么运作**：跳到 u4-l1（同步 FIFO），对照本讲学到的 `module_fifo.py` 的 `setup_vunit`/`get_build_projects`，去读它实际配置的 `fifo.vhd` 与 `tb_fifo.vhd`，你会立刻看到「Python 配置 ↔ VHDL 实体」的对应关系。
- **想深入验证方法论**：可以先读 u8-l2（VUnit 测试台模式），那里会更系统地讲 `add_vunit_config` 与 `tb_*.vhd` 的断言式自检如何配合。
- **继续阅读源码**：建议把 [modules/common/module_common.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py) 通读一遍——它是全项目最完整的 `module_*.py` 样本，几乎涵盖了所有常见的 generic 矩阵与资源断言写法。
