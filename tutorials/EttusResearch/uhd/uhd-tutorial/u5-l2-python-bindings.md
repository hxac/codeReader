# Python 绑定 pyuhd 与 pybind11

## 1. 本讲目标

UHD 的官方语言是 C++，但绝大多数现代 SDR 用户（GNU Radio、科研脚本、自动化测试）更习惯用 Python 控制 USRP。本讲要回答一个核心问题：**Python 里 `import uhd` 之后能用到的那些类和函数，到底是从哪里冒出来的？**

读完本讲，你应当能够：

- 说清 UHD Python API 的「两层结构」：编译型扩展 `libpyuhd` 与纯 Python 包 `uhd` 的分工。
- 看懂 `pyuhd.cpp` 这一个聚合入口如何把几十个 `export_*` 函数挂到一棵子模块树上。
- 掌握 pybind11 的典型包装手法：`py::class_`、`overload_cast`、lambda 适配、`return_value_policy` 与虚函数 trampoline。
- 区分一个 `uhd.xxx` 名字是「C++ 绑定直出」「纯 Python 重命名」还是「纯 Python 新逻辑」，并能动手追查它的导出链。

## 2. 前置知识

本讲是 **advanced** 阶段内容，建立在前几讲之上。开始前请确认你理解：

- **C++ ↔ Python 的根本鸿沟（承接 u5-l1）**：Python 解释器无法直接调用 C++ 的异常、`std::string`、`std::shared_ptr`，更不能直接识别 `uhd::usrp::multi_usrp` 这种 C++ 类。u5-l1 讲过的「C API 外壳 + 不透明句柄」是一种解法；本讲讲的是另一种更现代的解法——pybind11，它在编译期生成胶水代码，让 C++ 类「看起来像 Python 类」。
- **multi_usrp 与 rfnoc_graph（承接 u2-l3、u3-l1）**：你需要知道 `multi_usrp::make` 是高层设备门面、`rfnoc_graph::make` 是 RFNoC 会话入口，本讲的绑定工作主要就是把这两个对象（以及它们的成员函数）搬进 Python。
- **pybind11 是什么**：一个仅头文件（header-only）的 C++ 库，通过宏与模板，在编译一个共享库时生成 Python C 扩展模块。你不需要先学完 pybind11 再读本讲——本讲会结合 UHD 真实代码边讲边解释关键写法。
- **CMake target 的概念（承接 u1-l3）**：本讲会看到 `add_library(pyuhd SHARED ...)` 把若干 `.cpp` 编译成一个扩展库，并 `target_link_libraries(pyuhd uhd)` 链到主库。

一个关键直觉先放在这里：**用户 `import uhd` 拿到的 `uhd` 是一个纯 Python 包；这个包里的几乎所有真正的功能，都来自它内部 `from . import libpyuhd` 导入的那个编译扩展。** 记住这张「外壳 + 引擎」的图，本讲剩下的内容都是在拆解这张图。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `host/python/pyuhd.cpp` | 编译扩展 `libpyuhd` 的**唯一**聚合入口，`PYBIND11_MODULE` 宏所在地，把全部子模块挂上去。 |
| `host/python/uhd/__init__.py.in` | 纯 Python 包 `uhd` 的入口模板（CMake 用 `configure_file` 渲染成 `__init__.py`），负责导入 `libpyuhd` 并处理 Windows 下 DLL 查找。 |
| `host/python/uhd/usrp/multi_usrp.py` | 纯 Python 的 `MultiUSRP` 类，**继承**自 `lib.usrp.multi_usrp`，并追加 `recv_num_samps`/`send_waveform` 等便利方法。 |
| `host/python/uhd/rfnoc.py` | 纯 Python 模块，把 `lib.rfnoc.*` 重命名为 PEP8 风格的类名（`RfnocGraph`、`RadioControl` 等）。 |
| `host/python/uhd/chdr.py` | 纯 Python 模块，重命名 CHDR 解析类，并**猴子补丁**给 `ChdrPacket` 加 `get_payload` 方法。 |
| `host/python/uhd/types.py` | 纯 Python 模块，把 `lib.types.*` 重命名为 Python 风格类型（`TuneRequest`、`StreamCMD` 等）。 |
| `host/lib/usrp/multi_usrp_python.cpp` | C++ 绑定实现：`export_multi_usrp()` 把 C++ `multi_usrp` 包装成 Python 类。 |
| `host/lib/rfnoc/radio_control_python.hpp` | C++ 绑定实现：`export_radio_control()`，展示块控制器绑定与 feature 接口取回。 |
| `host/lib/rfnoc/rfnoc_python.hpp` | C++ 绑定实现：`export_rfnoc()`，含 `rfnoc_graph`、`mb_controller`、trampoline 类 `PyTimekeeper`。 |
| `host/lib/device_python.cpp` | C++ 绑定实现：`export_device()`，包装 `uhd::device::find`，并演示 GIL 释放。 |
| `host/python/CMakeLists.txt` | 构建脚本：把 `pyuhd.cpp` 等编成扩展库、确定输出文件名、安装 Python 包。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 两层架构**（先建立全局图景）→ **4.2 pybind11 绑定手法**（看引擎怎么造）→ **4.3 子模块组织与重命名约定**（看外壳怎么包引擎）→ **4.4 构建、numpy 与 GIL**（看引擎如何与 Python 运行时共存）。

### 4.1 两层架构：libpyuhd 引擎 + uhd 纯 Python 外壳

#### 4.1.1 概念说明

很多项目的 Python 绑定只有一层：一个编译扩展既是引擎也是用户接口。UHD 没有那么做，而是刻意分成两层：

- **引擎层 `libpyuhd`**：一个编译出来的共享库扩展（Windows 上是 `libpyuhd.pyd`，Linux 上形如 `libpyuhd.cpython-3xx-xxx.so`）。它的名字、里面的类名都「很 C++」，比如 `multi_usrp`、`rfnoc_graph`、`stream_cmd`。这一层只负责「忠实翻译」，不做任何 Python 体验优化。
- **外壳层 `uhd`**：一个**纯 Python** 包，目录在 `host/python/uhd/`。它 `import libpyuhd` 之后，把那些 C++ 风格的名字重新整理、重命名成符合 Python 习惯（PEP8 驼峰类名）的名字，并补充一些用纯 Python 写起来更方便的便利方法。

为什么要分两层？因为它把「机械翻译」和「体验设计」解耦了：

1. C++ 改一个函数签名，只需要改引擎层的绑定，外壳层的重命名不受影响。
2. 想给用户加一个 `recv_num_samps(freq, rate, ...)` 这样的小工具函数，用纯 Python 写在外壳里即可，不必动 C++。
3. 引擎层可以独立编译、独立加载，外壳层可以装到任意 site-packages。

#### 4.1.2 核心流程

用户执行 `import uhd` 时发生的事，可以用下面的伪代码描述：

```
import uhd
  → Python 执行 uhd/__init__.py
    → from .libpyuhd import find, get_version_string, ...   # 加载编译扩展
    → from . import usrp, rfnoc, types, chdr, ...            # 加载纯 Python 子模块
      → usrp/multi_usrp.py: class MultiUSRP(lib.usrp.multi_usrp)  # 继承引擎里的类
      → rfnoc.py:        RfnocGraph = lib.rfnoc.rfnoc_graph       # 重命名
      → types.py:        TuneRequest = lib.types.tune_request     # 重命名
  → uhd.usrp.MultiUSRP 现在可用，底层是 libpyuhd 里的 C++ multi_usrp
```

关键点：`libpyuhd` 是「根」，所有功能最终都落到它身上；`uhd` 包只是把根上的东西重新摆放得更好用。

#### 4.1.3 源码精读

先看引擎层的入口 `pyuhd.cpp`。整个扩展由一个宏声明，名字正是 `libpyuhd`：

[host/python/pyuhd.cpp:65-69](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/pyuhd.cpp#L65-L69) —— 宏 `PYBIND11_MODULE(libpyuhd, m)` 声明这个编译库对应 Python 模块名 `libpyuhd`，`m` 是模块对象；进入函数体后第一件事是 `init_numpy()` 初始化 numpy C API。

接着看外壳层入口。`__init__.py.in` 是 CMake 模板（`.in` 后缀，会被渲染成真正的 `__init__.py`），它的核心是这一段导入：

[host/python/uhd/__init__.py.in:58-65](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/uhd/__init__.py.in#L58-L65) —— `from .libpyuhd import find, get_abi_string, get_component, get_version_string` 把引擎层最顶层的几个函数直接提到 `uhd.find` 这样的位置；同时 `from . import chdr, dsp, filters, rfnoc, types, usrp, usrp_clock, usrpctl` 引入各纯 Python 子模块。

注意这里包了一层 `try/except ImportError`（第 66 行起）。这是给 **pymod-only 模式** 留的退路：当 UHD 用 `ENABLE_PYMOD_UTILS=ON` 但 `ENABLE_PYTHON_API=OFF` 编译时（即只想要纯 Python 工具子集、不编译 `libpyuhd`），导入 `libpyuhd` 会失败，此时若允许就降级成一个「没有引擎、只保留路径工具」的精简包：

[host/python/uhd/__init__.py.in:66-94](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/uhd/__init__.py.in#L66-L94) —— 若 `ENABLE_PYMOD_UTILS` 未开则原样抛出 ImportError；否则提供纯 Python 版本的 `get_pkg_path()` 等并设 `__version__`，整个包仍可导入但不带硬件能力。

这一段清楚地展示了「外壳可以脱离引擎存在」的设计意图。

#### 4.1.4 代码实践

**实践目标**：亲手验证「外壳包来自引擎扩展」这一结论。

**操作步骤**（源码阅读型，无需硬件）：

1. 在 `host/python/uhd/__init__.py.in` 第 60 行确认 `libpyuhd` 是被当作「当前包内的子模块」导入的（`from .libpyuhd import ...`），说明它和 `__init__.py` 安装在同一个目录。
2. 在 `host/python/CMakeLists.txt` 中找到编译产物是怎么落到那个目录的，确认 `libpyuhd` 与 `uhd/__init__.py` 确实被放到一起。
3. 思考：如果有人把 `libpyuhd.*` 文件删掉但保留 `uhd/` 目录，`import uhd` 会发生什么？

**需要观察的现象 / 预期结果**：

- CMakeLists 第 167-168 行有一条 `POST_BUILD` 命令，把编译出的 `pyuhd` target 拷贝到 `uhd/` 目录下。结合上面 `add_library` 的输出名前缀是 `lib`（见 4.4.3），该文件在包内就叫 `libpyuhd.*`，正好被 `__init__.py` 导入。
- 结论：删掉 `libpyuhd` 后，`import uhd` 会触发 `ImportError`；若编译时开了 pymod 退路，则降级为精简包。**待本地验证**：在有 UHD Python 安装的环境里执行 `python -c "import uhd; print(uhd.__file__)"` 与 `ls` 同目录，应能看到 `libpyuhd.*` 共存。

#### 4.1.5 小练习与答案

**练习 1**：为什么 UHD 不直接让用户 `import libpyuhd`，而要再包一层 `uhd`？

**参考答案**：直接用 `libpyuhd` 的话，类名会是 C++ 风格的下划线小写（`multi_usrp`、`stream_cmd`），且没有便利方法。`uhd` 外壳层把它们重命名为 Python 习惯的 `MultiUSRP`、`StreamCMD`，并用纯 Python 补充了 `recv_num_samps` 等工具函数；同时外壳解耦了「机械翻译」与「用户体验」，便于独立维护。

**练习 2**：`__init__.py.in` 里 `from .libpyuhd import find` 中的「`.`」指什么？

**参考答案**：指当前包（即 `uhd`）自身。它表示从 `uhd` 包目录下导入名为 `libpyuhd` 的子模块（即那个编译扩展文件），所以 `libpyuhd` 必须与 `uhd/__init__.py` 处于同一目录。

### 4.2 pybind11 绑定手法：把 C++ 类变成 Python 类

#### 4.2.1 概念说明

引擎层 `libpyuhd` 不是凭空生成的，而是 UHD 开发者用 pybind11 手写的一堆 `export_xxx()` 函数编译出来的。每个 `export_xxx` 负责把一组 C++ 实体「注册」到某个 Python 模块上。理解几种最常见的写法，你就能读懂 UHD 几乎所有绑定代码。

需要先认识的 pybind11 基本构件：

- `py::class_<T, Holder>(m, "Name")`：声明一个 Python 类 `Name`，它包装 C++ 类 `T`，`Holder`（通常是 `T::sptr`，即 `std::shared_ptr<T>`）说明对象的所有权持有方式。
- `.def(...)`：给这个类添加方法或构造函数。
- `py::arg("x") = 默认值`：声明参数名与默认值，这样 Python 侧可以按关键字传参。
- `py::overload_cast<...>(...)` / C 风格函数指针强转：用来从一组重载函数里选出特定签名的那一个。
- Lambda 包装：当 C++ 返回类型不方便直接暴露给 Python（比如返回 `std::map` 但带自定义比较器），就写一个 lambda 做类型转换。
- `py::return_value_policy::reference_internal`：控制返回指针的生命周期与所属关系，常用于「返回内部子对象的引用」。
- **Trampoline 类**：当 C++ 有虚函数（尤其是纯虚函数），而你想让 Python 能继承并覆写它时，需要一个中间类把调用「弹」回 Python。

#### 4.2.2 核心流程

以 `multi_usrp` 为例，绑定一个 C++ 类的典型流程是：

```
export_multi_usrp(m):                      # m = 目标 Python 模块
  1. py::class_<multi_usrp, multi_usrp::sptr>(m, "multi_usrp")
       → 生成一个叫 multi_usrp 的 Python 类，持有方式是 shared_ptr
  2. .def(py::init(&multi_usrp::make))     → 把工厂函数 make 暴露成构造函数
  3. .def("set_rx_rate", &multi_usrp::set_rx_rate, py::arg("rate"), py::arg("chan")=ALL_CHANS)
       → 暴露成员函数，带关键字参数与默认值
  4. .def("set_rx_gain", (void(*)(double,const std::string&,size_t))&multi_usrp::set_rx_gain, ...)
       → 对重载函数用「函数指针强转」选定具体重载
  5. .def("get_usrp_rx_info", [](self, chan=0){ return std::map<>(...); }, ...)
       → 用 lambda 把不方便暴露的返回类型转成 dict
```

最后在 `pyuhd.cpp` 里把这个 `export_multi_usrp` 挂到 `usrp` 子模块上即可。

#### 4.2.3 源码精读

**绑定一个高层门面类**。`multi_usrp_python.cpp` 把 C++ `uhd::usrp::multi_usrp` 暴露成 Python 类 `multi_usrp`：

[host/lib/usrp/multi_usrp_python.cpp:26-29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_python.cpp#L26-L29) —— `py::class_<multi_usrp, multi_usrp::sptr>(m, "multi_usrp")` 声明 Python 类，`multi_usrp::sptr`（即 `std::shared_ptr<multi_usrp>`）告诉 pybind11 用共享指针持有对象；`.def(py::init(&multi_usrp::make))` 把静态工厂 `multi_usrp::make` 注册成构造函数——所以 Python 里 `multi_usrp("type=x410")` 实际调用了 C++ 的 `make`。

**处理重载函数**。`set_rx_gain` 在 C++ 里有两个重载（一个带增益名，一个不带），需要告诉 pybind11 用哪一个。UHD 这里用的是 C 风格的函数指针强转：

[host/lib/usrp/multi_usrp_python.cpp:41-42](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_python.cpp#L41-L42) —— `(void (multi_usrp::*)(double, const std::string&, size_t)) &multi_usrp::set_rx_gain` 把成员函数指针强转到「三参数版本」，从而消除重载歧义；紧接着第 42 行再用「两参数版本」注册一次同名方法，Python 侧就能按传入参数个数自动分派。

**用 lambda 转换返回类型**。`get_usrp_rx_info` 在 C++ 返回一个带自定义比较器的 map，直接暴露给 Python 不方便，于是用 lambda 转成普通的 `std::map<std::string,std::string>`（pybind11 会自动把它变成 Python `dict`）：

[host/lib/usrp/multi_usrp_python.cpp:54-59](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp_python.cpp#L54-L59) —— lambda 接收 `self`（即 Python 对象对应的 C++ 引用）和 `chan`，把返回值 `static_cast` 成普通 `std::map`，配合 `py::arg("chan") = 0` 设默认值。

**绑定一个 RFNoC 块控制器 + feature 取回**。`radio_control_python.hpp` 展示了块控制器绑定，以及如何取回「feature 接口」（参考 u3-l2 提到的 discoverable feature 机制）：

[host/lib/rfnoc/radio_control_python.hpp:41-42](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_python.hpp#L41-L42) —— `py::class_<radio_control, noc_block_base, radio_control::sptr>(m, "radio_control")` 第二个模板参数 `noc_block_base` 声明 **基类**，于是 Python 侧 `radio_control` 自动「继承」`noc_block_base` 的方法（继承链见 u3-l2）；构造函数用 `block_controller_factory<radio_control>::make_from` 这个工厂模板。

[host/lib/rfnoc/radio_control_python.hpp:199-204](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/radio_control_python.hpp#L199-L204) —— `get_internal_sync` 用 lambda 调 `self.get_feature<...>()` 取回 feature 接口指针；关键是 `py::return_value_policy::reference_internal`——它告诉 pybind11「返回的是 self 内部的引用，生命周期由 self 拥有」，从而避免 Python 侧提前释放导致悬空指针。`get_tx_complex_gain`、`get_rx_complex_gain` 同理。

**Trampoline：让 Python 能覆写 C++ 纯虚函数**。`rfnoc_python.hpp` 给 `mb_controller::timekeeper`（一个含纯虚函数的类）写了 trampoline 类 `PyTimekeeper`：

[host/lib/rfnoc/rfnoc_python.hpp:38-65](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_python.hpp#L38-L65) —— `PyTimekeeper` 公有继承 `timekeeper`，每个纯虚函数（如 `get_ticks_now`）都用 `PYBIND11_OVERLOAD_PURE(...)` 宏把调用「弹」回 Python 子类的同名方法。这样用户在 Python 里继承 `timekeeper` 并实现 `get_ticks_now` 时，C++ 多态调用会正确路由到 Python 实现。

**隐式类型转换**。为了让 Python 用户可以直接传字符串当 `block_id` 用，绑定里注册了隐式转换：

[host/lib/rfnoc/rfnoc_python.hpp:127](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/rfnoc/rfnoc_python.hpp#L127) —— `py::implicitly_convertible<std::string, block_id_t>()` 声明：凡是期望 `block_id_t` 的地方，Python 传入 `str` 时自动构造一个 `block_id_t`，于是 `graph.connect("Radio#0", ...)` 这种写法可行。

#### 4.2.4 代码实践

**实践目标**：在真实绑定代码里识别四种 pybind11 手法。

**操作步骤**（源码阅读型）：

1. 打开 `host/lib/usrp/multi_usrp_python.cpp`，分别找出：① 用 `py::init` 注册工厂的一行；② 用函数指针强转解决重载的一行；③ 用 lambda 转换返回类型的一处；④ 用 `py::arg(...) = ...` 设默认值的一处。
2. 打开 `host/lib/rfnoc/radio_control_python.hpp`，找出用 `py::overload_cast`（与 4.2.3 的强转写法不同的一种重载消歧）的例子（提示：在 `get_tx_gain_range` 附近）。
3. 对比两种重载消歧写法：函数指针强转 vs `py::overload_cast`，思考它们各自的优缺点。

**需要观察的现象 / 预期结果**：

- `radio_control_python.hpp` 第 64-72 行用了 `py::overload_cast<const size_t>(&radio_control::get_tx_gain_range, py::const_)`：`overload_cast` 通过模板参数列表指定参数类型来消歧，可读性比 C 风格强转好，且能附带 `py::const_` 标记 const 成员函数。两种写法在 UHD 里混用，`overload_cast` 是 pybind11 推荐的新写法。
- 结论：看到 `.def("name", &Class::method, ...)` 多次同名出现，多半是在分别注册不同重载。

#### 4.2.5 小练习与答案

**练习 1**：`py::class_<radio_control, noc_block_base, radio_control::sptr>` 这三个模板参数分别是什么含义？

**参考答案**：第一个 `radio_control` 是被包装的 C++ 类；第二个 `noc_block_base` 声明它的 C++ 基类，使 Python 侧自动继承基类绑定的方法；第三个 `radio_control::sptr`（`shared_ptr`）是持有方式，决定对象在 Python/C++ 间的所有权与生命周期。

**练习 2**：为什么 `get_internal_sync` 的 lambda 必须带 `py::return_value_policy::reference_internal`？去掉会怎样？

**参考答案**：因为它返回的是 `radio_control` 内部持有的 feature 接口指针，并非独立分配的新对象。`reference_internal` 把返回对象的生命周期绑定到 `self`（即外层 `radio_control` 对象），确保只要 `radio_control` 还活着，feature 引用就有效。去掉的话 pybind11 可能按默认策略尝试接管或拷贝一个它并不拥有的指针，导致悬空引用或双重释放。

### 4.3 子模块组织与重命名约定

#### 4.3.1 概念说明

引擎层 `libpyuhd` 内部按 `def_submodule` 分了若干子模块（`types`/`usrp`/`rfnoc`/`cal`/`chdr`/`paths`/`filters`/`usrp_clock`），但里面的类名是 C++ 风格（全小写、下划线）。外壳层 `uhd` 包的职责之一，就是把这些名字重排成 Python 程序员习惯的样子。

UHD 外壳层处理引擎名字有三种典型手段，理解这三种手段，就能快速判断「一个 `uhd.xxx` 是怎么来的」：

1. **重命名（alias）**：直接 `NewName = lib.xxx.old_name`。纯赋值，不改变行为，只换名字。`rfnoc.py`、`types.py`、`libtypes.py` 多是这种。
2. **继承增强**：定义一个 Python 类**继承**引擎里的类，在子类里加新方法。`multi_usrp.py` 的 `MultiUSRP` 就是这种。
3. **猴子补丁（monkey patch）**：直接给引擎类**动态追加**方法。`chdr.py` 给 `ChdrPacket` 追加 `get_payload` 就是这种。

#### 4.3.2 核心流程

一个名字从 C++ 到用户手中的「改名流水线」：

```
C++ 类 uhd::types::tune_request
  → 引擎 libpyuhd: export_tune(types_module) 注册为 lib.types.tune_request
  → 外壳 uhd/types.py: TuneRequest = lib.types.tune_request   # 重命名
  → 用户: from uhd import types; types.TuneRequest(freq)
```

对比 RFNoC：

```
C++ 类 uhd::rfnoc::rfnoc_graph
  → 引擎 libpyuhd: export_rfnoc(rfnoc_module) 注册为 lib.rfnoc.rfnoc_graph
  → 外壳 uhd/rfnoc.py: RfnocGraph = lib.rfnoc.rfnoc_graph
  → 用户: uhd.rfnoc.RfnocGraph("type=x410")
```

注意「同物多名」：`lib.rfnoc.rfnoc_graph`、`uhd.rfnoc.RfnocGraph` 指向**同一个**底层 C++ 类，只是外壳给它起了个 Python 化的别名。

#### 4.3.3 源码精读

**重命名式**：`rfnoc.py` 把一整组 RFNoC 类集中重命名：

[host/python/uhd/rfnoc.py:10-35](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/uhd/rfnoc.py#L10-L35) —— 开头 `from . import libpyuhd as lib`，随后逐行 `RfnocGraph = lib.rfnoc.rfnoc_graph`、`RadioControl = lib.rfnoc.radio_control`、`DdcBlockControl = lib.rfnoc.ddc_block_control`……把 C++ 下划线名一一对应成 Python 驼峰名。文件末尾第 37-38 行还把两个自由函数 `connect_through_blocks`、`get_block_chain` 也搬过来。

`types.py` 做的事完全一样，只是针对 `lib.types.*`：

[host/python/uhd/types.py:13-24](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/uhd/types.py#L13-L24) —— `StreamCMD = lib.types.stream_cmd`、`TuneRequest = lib.types.tune_request`、`TimeSpec = lib.types.time_spec` 等。注意 `GainRange = FreqRange = lib.types.meta_range_t` 这种多个 Python 名字共享同一个底层类的写法——纯粹是为了让 API 在语义上更直观。

**继承增强式**：`multi_usrp.py` 的 `MultiUSRP` 继承引擎类并大幅扩展：

[host/python/uhd/usrp/multi_usrp.py:29-42](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/uhd/usrp/multi_usrp.py#L29-L42) —— `class MultiUSRP(lib.usrp.multi_usrp)` 直接继承引擎里的 `multi_usrp`；`__init__` 里 `super().__init__(args)` 调用引擎构造（即 C++ `make`），随后**用 Python 逻辑**检测这是不是 MPM 设备，若是就动态给实例挂上 `get_mpm_client` 方法（`setattr(self, ...)`）。这是纯 C++ 绑定做不到的灵活扩展。

接着看它新增的便利方法 `recv_num_samps`：

[host/python/uhd/usrp/multi_usrp.py:44-65](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/uhd/usrp/multi_usrp.py#L44-L65) —— 这是一个**纯 Python** 编写的「抓一段样本」便利函数，内部用 `lib.usrp.stream_args`、`lib.types.stream_cmd`、`lib.types.rx_metadata` 等引擎对象拼装出配置采集的完整流程，最后把结果装进 numpy 数组返回。用户因此能用一行 `usrp.recv_num_samps(1000, 2.4e9)` 完成原本几十行的配置工作。

**猴子补丁式**：`chdr.py` 在重命名之外，还给引擎类动态追加方法：

[host/python/uhd/chdr.py:6-43](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/uhd/chdr.py#L6-L43) —— 先重命名 `ChdrPacket = lib.chdr.ChdrPacket` 等；然后在文件级定义函数 `__get_payload(self)`，它根据包头类型分派到 `get_payload_mgmt()`/`get_payload_ctrl()`/...（这些 C++ 方法已在引擎里），最后第 43 行 `ChdrPacket.get_payload = __get_payload` 把它**绑到引擎类上**。于是所有 `ChdrPacket` 实例都获得了这个统一的 `get_payload` 入口。这是用纯 Python 给 C++ 类「补」一个高层方法，避免在 C++ 侧改动。

#### 4.3.4 代码实践

**实践目标**：把本节三种处理手段在真实代码里各找一例，建立「分类反射」。

**操作步骤**（源码阅读型）：

1. 在 `host/python/uhd/` 下用编辑器搜索 `= lib.`（重命名式）、`class.*(lib.`（继承式）、`\.get_payload =` 或 `setattr`（猴子补丁式）三种模式，各列出一个文件与行号。
2. 对你找到的继承式例子（很可能是 `MultiUSRP`），打开它，确认子类新增的方法里调用了哪些 `lib.*` 引擎对象。
3. 思考：`uhd.types.TuneRequest` 与 `uhd.rfnoc.RfnocGraph`，哪个是「重命名」、哪个也「只是重命名」？二者背后是否都是单纯的赋值？

**需要观察的现象 / 预期结果**：

- 重命名：`types.py` 的 `TuneRequest = lib.types.tune_request`；继承：`multi_usrp.py` 的 `class MultiUSRP(lib.usrp.multi_usrp)`；猴子补丁：`chdr.py` 的 `ChdrPacket.get_payload = __get_payload`。
- 二者都是单纯赋值式重命名——`RfnocGraph` 在 `rfnoc.py` 里只是 `= lib.rfnoc.rfnoc_graph`，并没有继承扩展。区分关键看是「直接赋值」还是「`class` 定义体」。

#### 4.3.5 小练习与答案

**练习 1**：用户写 `uhd.usrp.MultiUSRP("type=x410")`，请追踪这条调用最终落到哪个 C++ 函数。

**参考答案**：`MultiUSRP` 定义在 `multi_usrp.py`，继承 `lib.usrp.multi_usrp`；`MultiUSRP.__init__` 调 `super().__init__(args)`，即引擎类 `multi_usrp` 的构造，而后者在 `multi_usrp_python.cpp:29` 被绑定为 `py::init(&multi_usrp::make)`。所以最终调用 C++ 的 `uhd::usrp::multi_usrp::make(args)`。

**练习 2**：`types.py` 里 `GainRange = FreqRange = lib.types.meta_range_t` 为什么让两个名字指向同一个类？

**参考答案**：纯粹是 API 易用性考虑。增益范围、频率范围在数据结构上都是「范围」（`meta_range_t`），但语义不同。给同一个 C++ 类多个语义化别名，让用户写 `types.FreqRange` 时代码意图更清晰，而不必在 C++ 侧为每种语义各造一个类。

### 4.4 构建、numpy 与 GIL：引擎如何与 Python 运行时共存

#### 4.4.1 概念说明

引擎层 `libpyuhd` 是一个被 Python 解释器加载的扩展模块，它必须遵守 Python 扩展的两条铁律，否则会段错误。本模块解释这两条铁律在 UHD 代码里的体现，以及扩展如何被编译出来。

- **numpy C API 初始化铁律**：UHD 的收发接口直接把 numpy 数组作为样本缓冲（zero-copy 思想），这要求扩展在导入时调用 `import_array()` 初始化 numpy 的 C API，否则任何涉及 numpy 缓冲的操作都会段错误。
- **GIL（全局解释器锁）铁律**：Python 默认同一时刻只有一个线程执行 Python 字节码。当 C++ 进入一个长时间计算（比如 `device::find` 会阻塞数秒去网络上搜索设备）时，若不释放 GIL，其他 Python 线程会被完全卡死。负责的做法是：进入 C++ 长操作前释放 GIL，返回前重新获取。

此外还要理解扩展的**命名与构建**：Python 扩展模块的文件名有严格约定（带 `cpython-3xx` 等标签），UHD 用 CMake 拼出正确的名字，并保证它落到 `uhd` 包目录里被 `__init__.py` 找到。

#### 4.4.2 核心流程

```
CMake 构建:
  add_library(pyuhd SHARED pyuhd.cpp multi_usrp_python.cpp ...)
    → PREFIX "lib" + SUFFIX = Python 扩展后缀  → 输出 libpyuhd.<ext>
    → target_link_libraries(pyuhd uhd)         → 链到主库 libuhd
    → POST_BUILD 拷贝到 uhd/ 目录             → 与 __init__.py 同目录

Python 导入 libpyuhd 时:
  PYBIND11_MODULE(libpyuhd, m) 体首行 init_numpy()
    → import_array() 初始化 numpy C API  （此后 recv/send 可用 numpy 缓冲）

运行期长操作:
  uhd.find(hint) → export_device 的 lambda
    → py::gil_scoped_release release;   → 释放 GIL
    → uhd::device::find(hint)           → 阻塞搜索设备，期间其他 Python 线程可运行
    → （作用域结束自动重新获取 GIL）→ 返回结果
```

#### 4.4.3 源码精读

**numpy 初始化**。`pyuhd.cpp` 模块体的第一件事就是初始化 numpy：

[host/python/pyuhd.cpp:59-69](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/pyuhd.cpp#L59-L69) —— `init_numpy()` 内部调 `import_array()`（numpy 宏）；注释解释了为什么要包一层函数（新版 Python 里 `import_array` 返回 NULL 的处理）。随后在 `PYBIND11_MODULE(libpyuhd, m)` 体的首行（第 69 行）调用它。CMake 里也专门探测了 numpy 头文件目录并加入 include 路径（见 `CMakeLists.txt` 第 114-117 行的 `PYTHON_NUMPY_INCLUDE_DIR`）。

**GIL 释放**。`device_python.cpp` 包装 `uhd::device::find` 时显式释放 GIL：

[host/lib/device_python.cpp:16-23](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/device_python.cpp#L16-L23) —— lambda 里先 `py::gil_scoped_release release;` 释放 GIL，再调用 `uhd::device::find(hint)`。注释点明动机：`find` 会阻塞，若不释放 GIL，其他 Python 线程会被冻住。`release` 对象析构时自动重新获取 GIL。

**扩展的命名与构建**。`CMakeLists.txt` 把若干绑定 `.cpp` 编成一个共享库，并精心设置文件名前缀后缀：

[host/python/CMakeLists.txt:120-126](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/CMakeLists.txt#L120-L126) —— `add_library(pyuhd SHARED pyuhd.cpp ... multi_usrp_python.cpp ...)` 把聚合入口与几个直接编译进库的绑定文件列在一起（注意：大多数 `export_*` 实现在头文件里，通过 `pyuhd.cpp` 的 `#include` 被一起编译，故这里只列了少数 `.cpp`）。

[host/python/CMakeLists.txt:141-145](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/CMakeLists.txt#L141-L145) —— 非 Windows 下，`set_target_properties` 设 `PREFIX "lib"`，并用 `sysconfig.get_config_var('EXT_SUFFIX')` 取得 Python 扩展的标准后缀（如 `.cpython-311-x86_64-linux-gnu.so`）。两者拼起来正是 `libpyuhd.cpython-...so`，与 `__init__.py` 里 `from .libpyuhd import ...` 完全对应。

[host/python/CMakeLists.txt:167-168](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/python/CMakeLists.txt#L167-L168) —— `POST_BUILD` 把刚编译出的 `pyuhd` target 文件拷到 `uhd/` 目录，确保扩展与包入口同目录，从而能被 `from .libpyuhd import` 找到。

#### 4.4.4 代码实践

**实践目标**：确认「扩展文件名」「包入口导入名」「CMake target」三者如何对应。

**操作步骤**（源码阅读型）：

1. 在 `host/python/CMakeLists.txt` 里找到 `add_library` 的 target 名（`pyuhd`）、`PREFIX`（`lib`）、`SUFFIX`（取自 `EXT_SUFFIX`），三者拼接得到磁盘文件名 `libpyuhd.<ext>`。
2. 在 `host/python/uhd/__init__.py.in` 里找到 `from .libpyuhd import`，确认包内导入名与磁盘文件名（去掉扩展后缀）一致。
3. 在 `host/lib/device_python.cpp` 找到 `py::gil_scoped_release`，说明它为何出现在 `find` 而不是出现在 `get_rx_freq` 这种快速调用里。

**需要观察的现象 / 预期结果**：

- target 名 `pyuhd` + 前缀 `lib` → 文件名以 `libpyuhd` 开头；Python 按模块名 `libpyuhd` 导入，二者吻合。
- `find` 是「可能阻塞数秒」的网络搜索操作，释放 GIL 可让其他线程继续；而 `get_rx_freq` 只是读一个寄存器/缓存值、微秒级返回，没必要也不应释放 GIL（频繁加解锁反而有开销）。**待本地验证**：在有硬件或 simulator 的环境里，开两个 Python 线程，一个跑 `uhd.find`，另一个持续 `print`，可观察到 `find` 期间另一线程不被阻塞。

#### 4.4.5 小练习与答案

**练习 1**：如果删除 `pyuhd.cpp` 里的 `init_numpy()` 调用，会出什么问题？

**参考答案**：numpy 的 C API 未初始化，凡是用 numpy 数组作样本缓冲的收发操作（`recv`/`send`）在访问数组内存时会段错误。`import_array()` 必须在模块加载时执行一次，才能安全使用 numpy C API。

**练习 2**：为什么 CMake 要用 `sysconfig.get_config_var('EXT_SUFFIX')` 而不是直接用 `.so`？

**参考答案**：Python 3 扩展模块的文件名带解释器版本与平台标签（如 `.cpython-311-x86_64-linux-gnu.so`），Python 只会按这个标签去识别扩展模块。若强行用裸 `.so`，某些平台/版本下 `import` 会找不到模块。`EXT_SUFFIX` 正是当前解释器期望的标准后缀。

## 5. 综合实践

把本讲四个模块串起来，完成一次「**Python API 名字溯源**」。

**任务**：从一个 Python 侧的常用名字出发，一路追到它的 C++ 根源，并标注一路上经过的每一层。

**示例对象**：`uhd.usrp.MultiUSRP` 与 `uhd.rfnoc.RfnocGraph`。

**操作步骤**：

1. **定位外壳入口**：从 `host/python/uhd/__init__.py.in` 的 `from . import ... usrp ... rfnoc ...` 出发，确认 `uhd.usrp`、`uhd.rfnoc` 子模块的来源。
2. **判定处理手段**：打开 `host/python/uhd/usrp/multi_usrp.py` 与 `host/python/uhd/rfnoc.py`，判定 `MultiUSRP` 是「继承增强」、`RfnocGraph` 是「重命名」，分别给出关键行。
3. **跳到引擎层**：两者最终都指向 `libpyuhd` 里的 `lib.usrp.multi_usrp` 与 `lib.rfnoc.rfnoc_graph`。
4. **找到绑定注册点**：在 `host/lib/usrp/multi_usrp_python.cpp` 与 `host/lib/rfnoc/rfnoc_python.hpp` 找到 `py::class_<...>(m, "...")` 那一行，确认 Python 类名与被包装的 C++ 类。
5. **找到聚合挂载点**：回到 `host/python/pyuhd.cpp`，找到 `export_multi_usrp(usrp_module)` 与 `export_rfnoc(rfnoc_module)` 这两行，确认它们各自被挂到哪个 `def_submodule` 上。
6. **画出链路图**：用一张表或流程图，把每个名字在「C++ 类 → 引擎注册 → 外壳处理 → 用户接口」四列里的形态填出来。

**预期结果**（示例，行号以本讲引用为准）：

| 用户接口 | 外壳处理（文件:行） | 引擎注册（文件:行） | C++ 根源 |
| --- | --- | --- | --- |
| `uhd.usrp.MultiUSRP` | 继承 `multi_usrp.py:29` | `multi_usrp_python.cpp:26-29` | `uhd::usrp::multi_usrp` |
| `uhd.rfnoc.RfnocGraph` | 重命名 `rfnoc.py:17` | `rfnoc_python.hpp:181` | `uhd::rfnoc::rfnoc_graph` |

**进阶（可选）**：再挑一个 `uhd.types.TuneRequest`、`uhd.chdr.ChdrPacket`，分别走一遍同样的溯源，体会三种外壳处理手段（重命名 / 继承 / 猴子补丁）的差异。

## 6. 本讲小结

- UHD Python API 是**两层结构**：编译扩展 `libpyuhd`（引擎，忠实翻译 C++）+ 纯 Python 包 `uhd`（外壳，重命名与增强）。用户 `import uhd` 拿到的是外壳，所有功能最终落到引擎。
- `pyuhd.cpp` 是引擎的**唯一聚合入口**：一个 `PYBIND11_MODULE(libpyuhd, m)` 内，通过几十个 `export_xxx()` 函数把 C++ 实体挂到 `types`/`usrp`/`rfnoc`/`cal`/`chdr` 等子模块上。
- pybind11 包装有四大典型手法：`py::class_<T, Base, Holder>` 声明类与继承、`py::init` 注册工厂、函数指针强转或 `overload_cast` 消解重载、lambda 配合 `return_value_policy` 适配返回类型与生命周期；含虚函数的类还需 trampoline（如 `PyTimekeeper`）。
- 外壳处理引擎名字有三种手段：**重命名**（`rfnoc.py`/`types.py`）、**继承增强**（`MultiUSRP`）、**猴子补丁**（`chdr.py` 给 `ChdrPacket` 加 `get_payload`）。
- 扩展必须遵守 Python 运行时两条铁律：导入时 `init_numpy()`/`import_array()` 初始化 numpy C API；进入长操作前用 `py::gil_scoped_release` 释放 GIL（见 `device_python.cpp` 的 `find`）。
- CMake 用 `add_library(pyuhd SHARED ...)` + `PREFIX "lib"` + `EXT_SUFFIX` 拼出 `libpyuhd.<ext>`，再 `POST_BUILD` 拷到 `uhd/` 目录，使扩展与包入口同目录、相互对应。

## 7. 下一步学习建议

- **横向对比 C API（u5-l1）**：本讲的 pybind11 绑定与 u5-l1 的纯 C 外壳是「同一问题的两种解法」。建议对照阅读，体会 pybind11 如何省去手写句柄注册表、错误码翻译等大量样板。
- **深入 RFNoC Python 用法（承接 u3-l1～u3-l6）**：现在你已知道 `uhd.rfnoc.RfnocGraph`、`RadioControl`、`DdcBlockControl` 的来源，可去 `host/examples/`（如 RFNoC 相关示例）和 `host/python/uhd/rfnoc_utils/` 看真实的 RFNoC 流图脚本如何组合这些类。
- **动手加一个绑定**：找一个新的 RFNoC 块控制器（`host/lib/rfnoc/` 下任意 `*_python.hpp`），模仿 `radio_control_python.hpp` 的结构，理解「新增一个块需要改哪些文件」（绑定头、`pyuhd.cpp` 的 include 与 `export_xxx` 调用、必要时外壳重命名）。
- **阅读 numpy 缓冲交互**：若对 zero-copy 收发感兴趣，可追踪 `recv_num_samps`（`multi_usrp.py`）里 numpy 数组如何传给引擎层 `streamer.recv`，并进一步看 rx_streamer 绑定如何把 numpy 缓冲映射到 C++（关联 u4-l1 的 convert 子系统与 u4-l2 的传输层）。
