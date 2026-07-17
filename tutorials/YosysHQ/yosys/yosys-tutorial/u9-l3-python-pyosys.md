# Python 绑定：pyosys

## 1. 本讲目标

本讲是「扩展 Yosys」系列的第三讲，承接 [u9-l2（C++ API：把 Yosys 作为库嵌入）](u9-l2-cpp-api-plugin.md)。u9-l2 讲的是「在自己的 C++ 程序里调用 `libyosys`」，本讲讲的是同一套 `libyosys` 能力如何被搬到 Python 里，也就是官方称为 **pyosys** 的 Python 绑定。

学完本讲，你应当能够：

1. 理解 pyosys 是如何被构建出来的：`pybind11` + 一个自动「包装代码生成器」`generator.py`，把 `kernel/rtlil.h` 等 C++ 头文件里的类机械地翻译成 Python 可用的对象。
2. 用 Python 脚本驱动 yosys：`import` 时自动 `yosys_setup()`，再用 `Design()` + `run_pass(cmd, design)` 跑综合，与 shell/`.ys` 脚本走的是同一条 `Pass::call` 路径。
3. 在 Python 里直接遍历、甚至修改 RTLIL 设计库（`Design` / `Module` / `Wire` / `Cell` / `SigSpec`），就像在写 C++ Pass 一样。
4. 用 Python 子类化 `ys.Pass`，写出一个能被 `run_pass` 调用的自定义命令，并理解为什么「在 Python 里自定义 Pass」只对 `Pass` 和 `Monitor` 两个类可行。

本讲只覆盖三个最小模块：**Python 脚本驱动**、**遍历 RTLIL**、**Python pass**。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（前面讲义已建立）：

- **RTLIL 与命令系统**：Yosys 一切综合都围绕内部表示 RTLIL（`Design → Module → Wire/Cell/SigSpec`）展开；所有命令都是 `Pass` 的子类，经全局表 `pass_register` 派发，入口是 `Pass::execute(args, design)`。参见 u2、u4。
- **`libyosys` 库接口**：u9-l2 讲过，`yosys_setup()` / `run_pass()` / `run_frontend()` / `run_backend()` / `shell()` 这组「驱动函数」就是命令行 `yosys` 程序 `main()` 内部用的同一组函数；库与 CLI 能力等价。
- **pybind11（只需直觉）**：pybind11 是一个「用 C++ 写 Python 扩展模块」的库。它的核心是 `PYBIND11_MODULE(name, m)` 宏定义一个模块，再用 `m.def(...)` 暴露自由函数、用 `py::class_<T>(m, "T").def(...)` 暴露类与成员函数。本讲会读到的 `wrappers_tpl.cc` 与自动生成的 `wrappers.cc` 就是这样的扩展模块。
- **Python 里的 C++ 容器映射**：pybind11 会把 `std::vector` 映射成可迭代对象、把 `std::map/std::unordered_map` 映射成 dict。Yosys 自研的 hashlib `dict`/`pool` 也被手动包成了行为近似的 Python 对象。

> 一句话定位：pyosys 不是「另一个 yosys」，而是 `libyosys` 的 Python 外壳——你在 Python 里调用的每一个对象、每一个方法，背后都是同一个 C++ 实现。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [pyproject.toml](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyproject.toml) | 声明 Python 构建后端、依赖（`pybind11`、`cxxheaderparser`），是 `pip install .` 的入口。 |
| [pyosys/generator.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py) | 「包装代码生成器」：解析 C++ 头文件，自动生成 `wrappers.cc` 里成百上千行 pybind11 绑定代码。 |
| [pyosys/wrappers_tpl.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc) | 手写模板：模块初始化（`import` 时 `yosys_setup()`）、`Pass`/`Monitor` 的「蹦床类」（让 Python 能覆写虚函数）。 |
| [pyosys/modinit.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/modinit.py) | `pyosys` 包的 `__init__`：设置 `RTLD_GLOBAL` 让符号可被共享库解析。 |
| [pyosys/hashlib.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/hashlib.h) | 把 yosys 自研容器 `dict`/`pool`/`idict` 包成 Python 友好对象（`items()`/`keys()`/`values()` 等）。 |
| [pyosys/build/local_backend.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/build/local_backend.py) | 自定义 PEP 517 构建后端：在 `pip install` 时跑 CMake 编译出 wheel。 |
| [examples/python-api/script.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/script.py) | 最小脚本驱动示例：读设计、综合、遍历单元、画图。 |
| [examples/python-api/pass.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/pass.py) | 自定义 Python Pass 示例：子类化 `ys.Pass`。 |
| [docs/source/code_examples/pyosys/simple_database.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/pyosys/simple_database.py) | 官方文档示例：手工创建 `Design`、检查/修改设计库。 |
| [docs/source/using_yosys/pyosys.rst](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/pyosys.rst) | 官方 pyosys 使用指南。 |

## 4. 核心概念与源码讲解

本讲按「先把 yosys 当脚本跑 → 再直接摸 RTLIL → 最后把逻辑封装成 Pass」的顺序，分三个最小模块讲解。

### 4.1 Python 脚本驱动

#### 4.1.1 概念说明

`pyosys` 是 `libyosys` 的 Python 绑定。它给你两条能力：

1. **当脚本用**：像 `.ys` / `.tcl` 一样，用字符串发命令（`run_pass("opt -full")`），但额外享受 Python 的类型系统、控制流、函数与库生态。
2. **当 API 用**：可以手工 `new` 一个 `Design`，传给 `run_pass`，再把它的 `Module`/`Cell` 当 Python 对象读写——这正是 `.ys`/`.tcl` 做不到、而 pyosys 相对它们的「进阶」价值。

官方文档把它定义为「libyosys 的一个有限子集」：并非所有 C++ API 都暴露，但 RTLIL 数据结构与驱动函数都可用。

关键直觉：**`import` 的那一刻，`yosys_setup()` 就被自动调用了**——预填 `IdString`、注册所有内置 Pass、创建全局 `yosys_design`。所以你不需要像 u9-l2 的 C++ 程序那样手动 `yosys_setup()`；模块卸载（解释器退出）时会经一个 `py::capsule` 自动 `yosys_shutdown()`。

#### 4.1.2 核心流程

一段最简单的 pyosys 脚本，运行流程是：

```
Python 解释器启动
  └─ import pyosys.libyosys          # 触发扩展模块初始化
       ├─ 若 yosys 未初始化：
       │    ├─ yosys_setup()          # 注册全部 pass、建全局 design
       │    └─ 注册 _cleanup_handle capsule（退出时 yosys_shutdown）
       └─ 绑定所有类 / 自由函数 / 容器
  └─ design = ys.Design()             # 显式 new 一个空的 RTLIL::Design
  └─ ys.run_pass("read_verilog a.v", design)   # 字符串交给 Pass::call
  └─ ys.run_pass("prep", design)
  └─ ys.run_pass("write_verilog b.v", design)
```

注意第二行：`ys.Design()` 是**显式**创建一个 design 对象，之后每一次 `run_pass` 都把它作为第二个参数传进去。这和 u9-l2 讲的「库方式」一致——`run_pass(cmd, design)` 操作的是你传进去的那个 design，而不是全局的 `yosys_design`。这一点很重要：它让你可以同时持有多个互不干扰的设计。

> 与 `yosys -y script.py` 的关系：`-y` 让 yosys 用内嵌的 Python 解释器跑你的脚本；而 `pip install pyosys` 后直接 `python3 script.py` 则是用系统 Python。两者 import 的是同一个 `libyosys` 扩展模块，行为一致。

#### 4.1.3 源码精读

**(1) 构建入口：`pyproject.toml`**

pyosys 用了一个**自定义 PEP 517 构建后端**，而不是默认的 setuptools：

[pyproject.toml:1-9](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyproject.toml#L1-L9) 声明构建依赖与后端。关键两行：`requires` 里有 `pybind11>=3,<4`（绑定框架）和 `cxxheaderparser`（C++ 头文件解析器，被 `generator.py` 用来读 `kernel/rtlil.h`）；`build-backend = "local_backend"` 指向 [pyosys/build/local_backend.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/build/local_backend.py)，它在 `pip install` 时调用 CMake（`-DYOSYS_WITH_PYTHON=ON` 等）把 yosys 编译进 wheel。

**(2) `import` 时做了什么：`wrappers_tpl.cc` 的模块初始化**

整个扩展模块的 C++ 入口是 `PYBIND11_MODULE(libyosys, m)`：

[pyosys/wrappers_tpl.cc:166-179](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L166-L179) 这段是「import 即 setup」的实现。`yosys_already_setup()` 为假时，把 `std::cout` 接进日志流、调用 `yosys_setup()`，再用一个 `py::capsule` 注册退出回调 `yosys_shutdown()`。也就是说，**pyosys 复用了 u9-l2 讲的那套生命周期函数**，只是把「何时 setup/shutdown」交给了 Python 的导入/卸载时机。

紧接着它绑定了一组日志自由函数（[wrappers_tpl.cc:182-189](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L182-L189)），所以 Python 里能直接 `ys.log(...)`、`ys.log_header(design, ...)`、`ys.log_error(...)`，效果和 C++ 的 `log()` 完全一致（参见 u4-l4 日志系统）。

**(3) 解决符号解析：`modinit.py`**

[pyosys/modinit.py:1-19](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/modinit.py#L1-L19) 里最关键的一行是 `sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)`。`libyosys` 是个大共享库，pyosys 扩展模块要能解析它的符号，必须让 `dlopen` 把符号放进**全局符号表**（`RTLD_GLOBAL`）。`__all__ = ["libyosys"]` 说明这个包对外只暴露一个名字。

**(4) 最小驱动脚本：`script.py`**

官方自带的 [examples/python-api/script.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/script.py) 浓缩了「脚本驱动」的全部要素：

[examples/python-api/script.py:3-11](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/script.py#L3-L11) —— 第 3 行 `from pyosys import libyosys as ys`（约定俗成起别名 `ys`）；第 8 行 `design = ys.Design()` 显式建空设计；第 9-11 行三条 `run_pass` 分别读 Verilog、`prep`、`opt -full`，每条都把 `design` 传进去。这三行等价于一段 `.ys` 脚本，但你现在可以用 `if`/`for` 把它们包起来。

注意第 9 行的路径 `../../tests/simple/fiedler-cooley.v` 是**相对当前工作目录**的，所以这个示例要从 `examples/python-api/` 目录里运行。

#### 4.1.4 代码实践

**实践目标**：亲手把 yosys 当 Python 库跑起来，验证「import 即 setup」。

**操作步骤**：

1. 先用 u1-l2 的方法构建带 Python 的 yosys（`cmake -B build -DYOSYS_WITH_PYTHON=ON` 等），或直接 `python3 -m pip install pyosys`（官方预编译 wheel，见 [pyosys.rst:45-54](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/pyosys.rst#L45-L54)）。
2. 把下面这段存为 `hello_pyosys.py`（示例代码，非项目原有文件）：

   ```python
   # 示例代码
   from pyosys import libyosys as ys

   design = ys.Design()
   ys.run_pass("read_verilog ../../examples/cmos/counter.v", design)
   ys.run_pass("prep", design)
   ys.run_pass("opt -full", design)
   ys.run_pass("stat", design)          # 打印单元统计到日志
   ys.run_pass("write_verilog counter_out.v", design)
   ```

3. 从仓库根目录运行 `python3 hello_pyosys.py`（注意调整 `read_verilog` 的相对路径，使其相对你运行时的工作目录）。

**需要观察的现象**：

- 终端会打印 `1. Executing ...` 式的日志标题与 `stat` 的单元统计表——和你在 `yosys` shell 里看到的一模一样，证明走的是同一套日志与 pass 系统（u4-l4）。
- 目录下生成 `counter_out.v`。

**预期结果**：脚本无异常退出，日志里能看到 `Printing statistics.` 段。若报 `ImportError: libyosys ...`，多半是没设 `RTLD_GLOBAL`（正常 `import pyosys` 会自动设）或 wheel 未装好。

> 若无法本地构建/安装，可降级为「源码阅读型实践」：对照 [script.py:8-11](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/script.py#L8-L11) 说明「`ys.Design()` 返回什么、`run_pass` 的第二个参数起什么作用」，并把结论写下来。具体运行输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 pyosys 脚本里每次 `run_pass` 都要把 `design` 作为第二个参数传进去？不传会怎样？

> **答案**：`run_pass(cmd, design)` 显式指定命令作用在哪个 design 上，让你能同时持有多个独立设计。若用不带 design 参数的重载（操作全局 `yosys_design`），脚本里多个 design 会互相串扰。pyosys 同时提供了 `design.run_pass(cmd)` 这种方法形式（见 4.2.3 的 Design 特殊绑定），效果等价。

**练习 2**：`from pyosys import libyosys` 之后，是否还需要手动调用 `yosys_setup()`？

> **答案**：不需要。`libyosys` 扩展模块在被 import 时会检查 `yosys_already_setup()`，未初始化则自动 `yosys_setup()`（[wrappers_tpl.cc:170-173](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L170-L173)），退出时由 capsule 自动 `yosys_shutdown()`。

---

### 4.2 遍历 RTLIL

#### 4.2.1 概念说明

「脚本驱动」只能发字符串命令，看不到 RTLIL 长什么样。pyosys 的真正威力在于：**你可以把 `Design` / `Module` / `Wire` / `Cell` / `SigSpec` 当成 Python 对象直接读写**，就像在 C++ 里写一个 Pass（参见 u3 讲的 RTLIL 编程接口）。

这之所以可能，是因为 pyosys 不是手工一个个类去绑的，而是用 `generator.py` 这台「自动包装机」**机械地把 `kernel/rtlil.h` 等头文件里的公开类与成员翻译成 pybind11 绑定代码**。所以你在 Python 里能用的方法，几乎就是 C++ 头文件里 `public:` 的那些。

但有两条重要的「映射规则」需要先建立直觉（官方文档 [pyosys.rst:111-131](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/pyosys.rst#L111-L131) 总结过）：

- `std::vector` 表现得像 Python 可迭代对象；
- hashlib `dict` 表现得像 Python `dict`（有 `.items()`/`.keys()`/`.values()`，但是**无序**，修改可能引起整体重排）；hashlib `pool` 表现得像 Python `set`（也是无序）。
- Python 的 `str` 在需要时会**自动转换**成 `IdString`（pybind 的 `implicitly_convertible`）。

另外一条官方警告必须牢记（[pyosys.rst:136-140](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/pyosys.rst#L136-L140)）：**修改设计库会使你之前拿到的 Python 引用失效**——pyosys 不会像 Java 那样把删除的对象强行续命，行为和写 C++ 一模一样。

#### 4.2.2 核心流程

在 Python 里遍历一个已综合设计的典型流程：

```
design = ys.Design()
ys.run_pass("read_verilog ...", design)
ys.run_pass("prep", design)                       # 产生 $ 单元
top = design.top_module()                          # 取顶层模块（Module 对象）
for cell in top.cells_.values():                   # cells_ 是 dict，遍历其值
    print(cell.type.str())                         # type 是 IdString，.str() 转回普通字符串
    for port, sig in cell.connections().items():   # connections() 返回 dict<端口名, SigSpec>
        print("  ", port.str(), "->", sig.as_string())
```

要点：

- `top_module()` 直接拿到 `RTLIL::Module*`；`cells_` / `wires_` 是 Module 的**公开字段**（`dict`），在 Python 里就是 dict 对象，可 `.items()` / `.values()`。
- `cell.type` 是 `IdString`（u3-l3），要转成普通字符串用 `.str()`；`IdString` 也实现了 `__str__`，所以 `str(cell.type)` 与 `print(cell.type)` 都行。
- `cell.connections()` 返回端口→信号的 dict（u3-l1）；每个值是 `SigSpec`，调 `.as_string()` 得到人类可读文本。

#### 4.2.3 源码精读

**(1) 哪些类被暴露？看 `generator.py` 的「清单」**

[pyosys/generator.py:137-231](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L137-L231) 是一份显式清单 `pyosys_headers`，列出要包装的头文件与类。其中最关键的是 `kernel/rtlil.h`：

[pyosys/generator.py:157-230](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L157-L230) 把 RTLIL 的几乎全部核心类都登记进来：`IdString`、`Const`、`AttrObject`、`NamedObject`、`Selection`、`CaseRule`/`SwitchRule`/`SyncRule`/`Process`、`SigChunk`/`SigBit`/`SigSpec`、`Cell`、`Wire`、`Memory`、`Module`、`Design`。每个类还可以带元数据，例如：

[pyosys/generator.py:160-176](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L160-L176) 给 `IdString` 配了 `string_expr="s.str()"`（决定 Python 的 `__str__`/`__repr__` 怎么打印）、`hash_expr="s.str()"`（决定 `__hash__`），以及一个 `denylist` 把不应从 Python 乱动的全局存储方法排除掉（如 `global_id_storage_`）。

**重要细节**：并非所有类都「能被 Python 构造」。`Cell` / `Wire` / `Memory` / `Module` / `Process` 都标了 `ref_only=True`（[generator.py:198-222](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L198-L222)），意思是 Python 只能**引用**它们、不能 `ys.Cell()` 凭空 new 一个——因为它们必须经由 `Module::addCell` 等工厂方法接纳才合法（u3-l1）。`Design` / `IdString` / `Const` / `SigSpec` 等则可构造。

**(2) 成员如何被翻译？`process_method` 的过滤规则**

[pyosys/generator.py:510-537](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L510-L537) 定义了「哪些方法会被包」：跳过 `deleted`/模板/变参/非 public/纯虚/析构/移动构造；构造函数仅在非 `ref_only` 时包成 `py::init<...>`。重载则用 `static_cast<返回类型 (Class::*)(参数) const>(&Class::method)` 精确选出一个（[generator.py:457-485](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L457-L485)）。这就是为什么 `cell.connections()`、`cell.setPort(...)`、`wire.width` 这些能直接在 Python 用。

运算符也被映射成 Python 魔术方法（[generator.py:540-550](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L540-L550)）：`==`→`__eq__`、`!=`→`__ne__`、`<`→`__lt__`、`const operator[]`→`__getitem__`。

**(3) `Design` 的一个手写补丁：`run_pass` 方法**

生成器对 `Design` 做了一处特殊处理：

[pyosys/generator.py:710-718](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L710-L718) 给 `Design` 额外绑了两个 `run_pass` 重载（接受 `std::string` 或 `std::vector<std::string>`），内部调 `Pass::call(&s, cmd)`。所以除了 4.1 讲的自由函数 `ys.run_pass(cmd, design)`，你也可以写 `design.run_pass(cmd)`——两条路最终都汇入 u4-l1 讲的 `Pass::call` 派发。

**(4) hashlib `dict` 在 Python 里长什么样**

`Cell::connections()` 返回 `dict<IdString, SigSpec>&`，这个 `dict` 是 yosys 自研容器（u3-l3），不能直接用 pybind11 内置映射，所以 [pyosys/hashlib.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/hashlib.h) 手写了一套绑定，把它包成「像 dict 的对象」：

[pyosys/hashlib.h:317-336](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/hashlib.h#L317-L336) 提供 `__len__`/`__getitem__`/`__setitem__`/`__contains__`/`__iter__`（键迭代）；并在下方补上 `.items()`/`.keys()`/`.values()`（[hashlib.h:349-395](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/hashlib.h#L349-L395)）。所以 `cell.connections().items()` 在 Python 里返回 `(端口名, SigSpec)` 对的迭代器，可像普通 dict 一样 `for k, v in ...:`。

**(5) 官方检查/修改示例：`simple_database.py`**

[docs/source/code_examples/pyosys/simple_database.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/pyosys/simple_database.py) 把「遍历」与「修改」都演示了。先看遍历部分：

[simple_database.py:13-28](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/pyosys/simple_database.py#L13-L28) —— `design.top_module()` 取顶层；`top_module.wires_.items()` 遍历所有线网（`wires_` 是 dict）；读 `wire.port_input`/`port_output`/`width`/`start_offset`/`upto`/`name.str()`，这些正是 u2-l3 讲过的 Wire 几何与端口属性。

修改部分更进一步，直接改单元类型与端口：

[simple_database.py:38-48](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/pyosys/simple_database.py#L38-L48) —— `top_module.addWire("\\enable")` 调 u3-l1 讲的工厂方法新建线（注意 `\` 前缀，因为它是公有名）；`enable_line.port_input = True` 后 `top_module.fixup_ports()` 让 yosys 重新处理端口；随后 `for cell in top_module.cells_.values()` 遍历单元，把 `$_DFF_P_` 改成 `$_DFFE_PP_` 并 `cell.setPort("\\E", ys.SigSpec(enable_line))` 接上使能。改完调 `top_module.check()` 做合法性校验（u3-l1）。这段示例充分说明：**Python 里对 RTLIL 的操作与 C++ Pass 里的写法几乎逐字对应**。

#### 4.2.4 代码实践

**实践目标**：遍历一个综合后设计的单元，理解 `cells_` / `connections()` / `as_string()` 的返回值。

**操作步骤**：

1. 准备一段脚本（示例代码）：

   ```python
   # 示例代码
   from pyosys import libyosys as ys

   design = ys.Design()
   ys.run_pass("read_verilog ../../examples/cmos/counter.v", design)
   ys.run_pass("prep", design)
   ys.run_pass("opt -full", design)

   for module in design.selected_whole_modules_warn():   # 当前选区里的整模块
       print("module:", module.name.str())
       for cell in module.cells_.values():
           print("  cell", cell.name.str(), "type", cell.type.str())
           for port, sig in cell.connections().items():
               print("     ", port.str(), "->", sig.as_string())
   ```

2. 从仓库根目录运行（按需调整 `read_verilog` 路径）。

**需要观察的现象**：每个 cell 一行类型（如 `$adff`、`$mux`、`$and` 等），下面缩进列出它的每个端口名（`\A`、`\B`、`\Y`、`\CLK` …）及所连信号文本（如 `\count [0]`、`1'0`）。

**预期结果**：能完整打印出 counter 综合后的门级结构。具体出现的 cell 类型与端口连接取决于 `prep`/`opt` 的结果，**待本地验证**。若某个 SigSpec 含 `x`/`z`，`.as_string()` 也会原样体现（u3-l3 讲过的位状态）。

> 对照阅读：[kernel/rtlil.h:2527](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2527) 是 `Cell::connections()` 的声明，[kernel/rtlil.h:1733](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L1733) 是 `SigSpec::as_string()` 的声明，其实现见 [kernel/rtlil.cc:5636](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.cc#L5636)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cell`、`wire` 不能用 `ys.Cell()` 直接构造，而必须用 `module.addCell(...)`？

> **答案**：这些类在 [generator.py:198-222](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L198-L222) 标了 `ref_only=True`，生成器据此跳过构造函数绑定。根本原因是 u3-l1 讲的归属规则：Cell/Wire 必须经 Module 接纳（建立反向指针 `module`、断言名字唯一）才合法，裸 new 一个没有归属、不合法。

**练习 2**：`cell.connections()` 在 Python 里返回的是什么？怎么遍历它的键和值？

> **答案**：返回一个 hashlib `dict`（C++ 的 `const dict<IdString, SigSpec>&`），在 Python 里被包成「像 dict 的对象」。可用 `.items()` 同时取端口名与 SigSpec，或 `.keys()`/`.values()` 单独取；也可 `port in cell.connections()` 判存在、`cell.connections()[port]` 取值。注意它是无序的（[pyosys.rst:122-127](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/pyosys.rst#L122-L127)）。

---

### 4.3 Python pass

#### 4.3.1 概念说明

4.1 用 `run_pass` 发字符串命令，4.2 用 Python 直接摸 RTLIL。这一步更进一层：**把一段 Python 逻辑封装成一个有名字的「命令」**，注册进 `pass_register`，之后既能在 Python 里 `run_pass("my_cmd")` 调它，也能在 yosys 脚本/交互 shell 里把它当普通命令用。

这正是 u9-l1（C++ 自定义 Pass）的 Python 对应物：派生一个 `ys.Pass` 子类、实现 `execute(args, design)`、实例化它即完成注册。区别只在于语言——Python 不用编译、不用 `yosys-config`，改完即用。

但有一个**硬限制**：pyosys 官方说，抽象类与虚函数「通常不支持」，只有两个例外——`Pass`（`kernel/register.h`）与 `Monitor`（`kernel/rtlil.h`）（[pyosys.rst:218-222](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/using_yosys/pyosys.rst#L218-L222)）。原因是这两个类用了 pybind11 的「蹦床（trampoline）」机制，允许 Python 子类覆写 C++ 的虚函数，并在 C++ 侧回调进 Python。

#### 4.3.2 核心流程

写一个 Python Pass 的最小骨架：

```
class MyPass(ys.Pass):
    def __init__(self):
        super().__init__("my_cmd", "一句话短说明")   # 命令名 + short_help
    def execute(self, args, design):                 # 必须实现（对应 C++ 的纯虚 execute）
        ys.log_header(design, "Running my_cmd\n")
        for module in design.all_selected_whole_modules():
            for cell in module.selected_cells():
                ...                                  # 遍历当前选区里的单元

p = MyPass()                                          # 实例化 → 自动注册进 pass_register
```

要点：

- `super().__init__(name, short_help)` 的两个参数对应 C++ `Pass` 构造函数（u4-l1、u9-l1）。
- `execute(self, args, design)` 是**纯虚**的 Python 实现，签名与 C++ 一致（`args` 是字符串列表，`design` 是 `Design`）。
- **实例化即注册**：构造函数内部会触发 `Pass::init_register()`，把这个 pass 搬进 `pass_register`——和 u9-l1 讲的「全局静态对象构造时入注册链」是同一套机制，只是触发点从「C++ 全局对象构造」变成「Python 实例化」。
- Pass 里要尊重**选择（selection）作用域**（u4-l3）：用 `design.all_selected_whole_modules()` / `module.selected_cells()` 只处理被选中的对象，而不是 `cells_` 全表——这样你的命令也能被 `select ... ; my_cmd` 限定范围。

#### 4.3.3 源码精读

**(1) 让 Python 能覆写虚函数：`PassTrampoline`**

[pyosys/wrappers_tpl.cc:46-87](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L46-L87) 是 pybind11 经典的「trampoline」模式（文件里的注释也指向了 pybind11 文档）。`PassTrampoline` 公有继承 `Pass`，把每个虚函数用 `PYBIND11_OVERRIDE` / `PYBIND11_OVERRIDE_PURE` 宏包起来：

[pyosys/wrappers_tpl.cc:62-70](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L62-L70) 对纯虚 `execute` 用 `PYBIND11_OVERRIDE_PURE`——意思是「调用时一定回到 Python 子类的 `execute`」；而 `help`/`clear_flags`/`on_register` 等用普通 `PYBIND11_OVERRIDE`，若 Python 没覆写就回落到 C++ 默认实现。这就是为什么 `pass.py` 里只实现了 `execute`、`help`、`clear_flags` 几个，其余照常可用。

`MonitorTrampoline`（[wrappers_tpl.cc:89-164](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L89-L164)）对 `RTLIL::Monitor` 做同样的事，于是 Python 也能写一个监听器，在设计被增删/改连接时收到回调。

**(2) 把 `Pass` 类绑出来，并在构造时注册**

[pyosys/wrappers_tpl.cc:195-217](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L195-L217) 把 `Pass` 绑成一个可被子类化的 Python 类（`py::class_<Pass, PassTrampoline, unique_ptr<Pass, nodelete>>`）。注意第 196-200 行的构造 lambda：

```cpp
.def(py::init([](std::string name, std::string short_help) {
    auto created = new pyosys::PassTrampoline(name, short_help);
    Pass::init_register();      // ← 关键：构造即把 pass 搬进 pass_register
    return created;
}), py::arg("name"), py::arg("short_help"))
```

这正是「实例化即注册」的物理证据：每次 Python 里 `ys.Pass("my_cmd", "...")`（即你 `super().__init__(...)`）都会 `new` 一个 trampoline 并立刻调 `init_register()`（u9-l1、u4-l1 讲过的去中心化注册机制）。`nodelete` 表示 yosys 不负责 delete 这个对象（它的生命周期由 Python 持有）。

> 注意：`Pass` 没有出现在 `generator.py` 的自动清单里（[generator.py:151-156](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L151-L156) 里 `register.h` 下的 `Pass` 被注释掉了，标注「Virtual methods, manually bridged」）。凡是含虚函数、需要被 Python 子类覆写的类，都走 `wrappers_tpl.cc` 这条手写路径，而非自动生成。

**(3) 完整示例：`pass.py`**

[examples/python-api/pass.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/pass.py) 是一个「画单元统计柱状图」的自定义 Pass：

[examples/python-api/pass.py:11-35](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/pass.py#L11-L35) —— `class CellStatsPass(ys.Pass)`；`__init__` 里 `super().__init__("cell_stats", "Shows cell stats as plot")`；`help()` 用 `ys.log(...)` 输出说明；`execute(self, args, design)` 用 `ys.log_header(design, ...)` 打标题，再 `design.all_selected_whole_modules()` → `module.selected_cells()` 遍历选区单元、按 `cell.type.str()` 统计每种类型的数量，最后用 matplotlib 画图。第 35 行 `p = CellStatsPass()` 一实例化，`cell_stats` 命令就注册好了。

[examples/python-api/pass.py:37-42](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/examples/python-api/pass.py#L37-L42) 是 `__main__` 部分：建 design、读设计、`prep`、`opt -full`，然后 `ys.run_pass("cell_stats", design)` 调用刚刚注册的命令。注意：要让 `cell_stats` 可用，必须先**导入/执行**定义它的那个模块（实例化 `CellStatsPass()`），所以这个示例把定义与调用放在同一个 `.py` 里。

#### 4.3.4 代码实践

**实践目标**：写一个能在 shell 里被 `help` 看到的自定义命令。

**操作步骤**：

1. 把 `pass.py` 的核心逻辑简化成一个不依赖 matplotlib 的「计数 pass」，存为 `count_cells.py`（示例代码）：

   ```python
   # 示例代码
   from pyosys import libyosys as ys

   class CountCellsPass(ys.Pass):
       def __init__(self):
           super().__init__("count_cells", "prints the number of cells per type")
       def help(self):
           ys.log("count_cells - prints how many cells of each type exist\n")
       def execute(self, args, design):
           ys.log_header(design, "Counting cells\n")
           stats = {}
           for module in design.all_selected_whole_modules():
               for cell in module.selected_cells():
                   t = cell.type.str()
                   stats[t] = stats.get(t, 0) + 1
           for t, n in sorted(stats.items()):
               ys.log(f"  {t}: {n}\n")

   CountCellsPass()   # 注册
   ```

2. 在同文件加 `__main__` 段：建 design → read_verilog → prep → `ys.run_pass("count_cells", design)`，运行它。

**需要观察的现象**：日志里先出现 `Counting cells` 标题，再列出每种 `$` 单元的数量。

**预期结果**：与 `stat` 命令的单元计数口径接近（但不完全等同，因为这里只数 `selected_cells()`）。具体数字**待本地验证**。

**思考延伸**：把 `CountCellsPass()` 的实例化放到一个单独模块 `my_passes.py`，再用 `yosys -m` 风格或先 `import my_passes` 的方式加载它，就能在任意 yosys 脚本里 `count_cells` 了——这与 u9-l1 的 C++ 插件 `.so` 在「去中心化注册」上是同一回事，只是分发载体从动态库变成了 Python 模块。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `execute` 用 `PYBIND11_OVERRIDE_PURE`，而 `help` 用普通 `PYBIND11_OVERRIDE`？

> **答案**：C++ 里 `Pass::execute` 是**纯虚**函数（无默认实现），任何 Pass 都必须提供，所以用 `PYBIND11_OVERRIDE_PURE` 强制回调 Python 子类（[wrappers_tpl.cc:62-70](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L62-L70)）。`help` 是普通虚函数、有默认实现，Python 没覆写时就回落到 C++ 默认 `Pass::help()`，因此用普通 `OVERRIDE`。

**练习 2**：pyosys 里 `Pass` 为什么是「手写绑定」而不是 `generator.py` 自动生成的？

> **答案**：自动生成器跳过纯虚函数（[generator.py:516-518](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py#L516-L518)），而且无法表达「让 Python 子类覆写 C++ 虚函数」的 trampoline。`Pass`（与 `Monitor`）恰恰需要被子类化并覆写 `execute`/`notify_*`，所以必须手写 `PassTrampoline`/`MonitorTrampoline`（[wrappers_tpl.cc:46-164](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/wrappers_tpl.cc#L46-L164)）。这也是官方说「只有这两个类支持虚函数」的根因。

**练习 3**：自定义 Pass 里为什么要用 `module.selected_cells()` 而不是 `module.cells_.values()`？

> **答案**：为了尊重选择（selection）作用域（u4-l3）。用户可能用 `select ... ; count_cells` 把命令限定在部分单元上；用 `selected_cells()` 只处理被选中的对象，命令才「选择感知」。直接遍历 `cells_` 会无视选择，等同于对全模块操作。

## 5. 综合实践

把本讲三个模块串起来：**用 `script.py` 的方式加载 `counter.v`、综合，然后遍历顶层模块的 cells，打印每个 cell 的 type 与端口连接**。

**操作步骤**：

1. 把下面这段存为 `dump_counter.py`（示例代码），放在仓库根目录：

   ```python
   # 示例代码：综合实践 —— 加载 counter.v 并打印每个 cell 的类型与端口连接
   from pyosys import libyosys as ys

   # —— 模块①：脚本驱动 ——
   design = ys.Design()
   ys.run_pass("read_verilog examples/cmos/counter.v", design)
   ys.run_pass("prep", design)        # 行为级 → $ 单元（字级综合，同 script.py）
   ys.run_pass("opt -full", design)

   # —— 模块②：遍历 RTLIL ——
   top = design.top_module()
   print("top module:", top.name.str())

   for cell in top.cells_.values():
       print(f"cell {cell.name.str()} : type = {cell.type.str()}")
       for port, sig in cell.connections().items():
           # port 是 IdString，sig 是 SigSpec；都转成可读字符串
           print(f"    {port.str()} -> {sig.as_string()}")
   ```

2. 运行：`python3 dump_counter.py`（需先按 4.1.4 装好 pyosys）。

3. （进阶）把上面的遍历逻辑重构成一个**自定义 Pass**（模块③）：定义 `class DumpCellsPass(ys.Pass)`，`execute` 里遍历 `design.all_selected_whole_modules()` 的 `selected_cells()`，注册后用 `ys.run_pass("dump_cells", design)` 调用。

**需要观察的现象 / 预期结果**：

- 先打印顶层模块名 `\counter`。
- 逐个打印每个 cell 的类型（`prep`/`opt` 后的通用 `$` 单元，如 `$adff`、`$mux`、`$and`、`$reduce_xxx` 等）及其每个端口的连接信号。
- 改用 `synth` 替代 `prep` 后，单元会进一步被 techmap/abc 映射成 `$_AND_`/`$_DFF_P_` 等门级原语（u6-l5、u6-l6），类型名会变化——可借此直观对比「字级综合」与「完整综合」的差异。

> 具体出现的 cell 类型与端口连接、以及 `synth` 后的形态，取决于设计与综合选项，**待本地验证**。

## 6. 本讲小结

- pyosys 是 `libyosys` 的 Python 绑定：`import pyosys.libyosys` 时会自动 `yosys_setup()`，退出时自动 `yosys_shutdown()`，与 u9-l2 的 C++ 库方式是同一套生命周期。
- 驱动方式与 shell/`.ys` 同源：`Design()` + `run_pass(cmd, design)` 最终都汇入 `Pass::call`；额外价值在于可用 Python 的类型/控制流，并能直接读写 RTLIL。
- RTLIL 的 C++ 类经由 `generator.py` 这台「自动包装机」机械翻译到 Python；`dict`/`pool` 经 [pyosys/hashlib.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/hashlib.h) 包成 Python 友好对象；`ref_only` 的类只能引用、不能裸构造。
- 在 Python 里写自定义 Pass 是可行的，但**仅限 `Pass` 与 `Monitor`**——它们靠 `wrappers_tpl.cc` 里的 trampoline 实现「Python 覆写 C++ 虚函数」，实例化即触发 `Pass::init_register()` 完成去中心化注册。
- 三种扩展方式可对照记忆：u9-l1 的 C++ 插件 `.so`（yosys 当宿主）、u9-l2 的 C++ 库嵌入（你的程序当宿主）、本讲的 Python 绑定（Python 当宿主）——三者注册与驱动机制同构。

## 7. 下一步学习建议

- **动手扩展综合流程**：参照 [simple_database.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/docs/source/code_examples/pyosys/simple_database.py) 的「修改设计库」部分，用 pyosys 给一个设计批量改单元类型/加端口，再 `write_verilog` 验证。
- **对比 C++ 与 Python 两种 Pass**：把本讲的 `CountCellsPass` 用 u9-l1 的 C++ 方式重写一遍，体会「同一注册机制、不同载体」。
- **深入绑定机制**：通读 [generator.py](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/generator.py) 的 `process_class` / `process_method` / `get_overload_cast`，理解「头文件 → pybind 绑定」的自动化原理；再看 [pyosys/CMakeLists.txt](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/pyosys/CMakeLists.txt) 里 `add_custom_command` 如何在构建时调用它生成 `wrappers.cc`。
- **回看主线**：本讲是「扩展 Yosys」单元（u9）的收尾。若想继续向「专家层」深入，可进入 u10（SAT/形式验证、functional IR/AIG、pmgen 模式匹配、FSM 与线程等内部机制）。
